from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Base


def get_or_create(
    session: Session,
    model: Base,
    defaults: dict | None = None,
    **filters,
) -> Base:
    instance = session.scalar(
        select(model).filter_by(**filters)
    )

    if instance is None:
        instance = model(**filters, **(defaults or {}))
        session.add(instance)
        session.flush()

    return instance