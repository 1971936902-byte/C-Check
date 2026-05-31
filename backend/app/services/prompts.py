from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import PromptVersion, User


DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "default_c_review.md"


def load_default_prompt() -> str:
    return DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").strip()


def get_active_prompt(db: Session) -> PromptVersion:
    prompt = db.scalar(select(PromptVersion).where(PromptVersion.is_active.is_(True)))
    if prompt is None:
        prompt = PromptVersion(version=1, body=load_default_prompt(), is_active=True)
        db.add(prompt)
        db.commit()
        db.refresh(prompt)
    return prompt


def create_prompt_version(db: Session, *, body: str, creator: User | None) -> PromptVersion:
    latest_version = db.scalar(select(PromptVersion.version).order_by(PromptVersion.version.desc()))
    prompt = PromptVersion(version=(latest_version or 0) + 1, body=body, creator=creator)
    db.add(prompt)
    db.commit()
    db.refresh(prompt)
    return prompt


def activate_prompt(db: Session, prompt: PromptVersion) -> PromptVersion:
    db.execute(update(PromptVersion).values(is_active=False))
    prompt.is_active = True
    db.commit()
    db.refresh(prompt)
    return prompt
