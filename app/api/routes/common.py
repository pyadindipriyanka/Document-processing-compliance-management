import math

from app.schemas.schemas import Page


def make_page(items, total: int, page: int, page_size: int, model) -> dict:
    return Page[model](items=[model.model_validate(i) for i in items], total=total, page=page, page_size=page_size,
                       pages=max(1, math.ceil(total / page_size)) if total else 0).model_dump()
