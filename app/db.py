from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from . import config

engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[str] = mapped_column(String(64), unique=True)  # OpenID "sub"
    name: Mapped[str] = mapped_column(String(255), default="")
    access_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime)  # UTC, naive

    posts: Mapped[list["Post"]] = relationship(back_populates="account", cascade="all, delete-orphan")

    @property
    def urn(self) -> str:
        return f"urn:li:person:{self.member_id}"

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= datetime.utcnow()


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    text: Mapped[str] = mapped_column(Text)
    image_paths: Mapped[str] = mapped_column(Text, default="")  # newline separated
    # draft | scheduled -> posting -> posted | failed
    status: Mapped[str] = mapped_column(String(16), default="scheduled", index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC, naive
    visibility: Mapped[str] = mapped_column(String(16), default="PUBLIC")  # PUBLIC | CONNECTIONS
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idem_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # API de-duplication
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    linkedin_post_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    account: Mapped[Account] = relationship(back_populates="posts")

    @property
    def images(self) -> list[str]:
        return [p for p in self.image_paths.split("\n") if p]

    @property
    def url(self) -> str | None:
        if self.linkedin_post_id:
            return f"https://www.linkedin.com/feed/update/{self.linkedin_post_id}"
        return None


def init_db() -> None:
    Base.metadata.create_all(engine)
    # Lightweight migration so an existing scheduler.db keeps working.
    cols = {c["name"] for c in inspect(engine).get_columns("posts")}
    with engine.begin() as conn:
        if "visibility" not in cols:
            conn.execute(text("ALTER TABLE posts ADD COLUMN visibility VARCHAR(16) DEFAULT 'PUBLIC'"))
        if "idem_key" not in cols:
            conn.execute(text("ALTER TABLE posts ADD COLUMN idem_key VARCHAR(64)"))
        if "attempts" not in cols:
            conn.execute(text("ALTER TABLE posts ADD COLUMN attempts INTEGER DEFAULT 0"))
