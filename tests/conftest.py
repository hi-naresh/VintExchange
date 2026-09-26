from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.models import RFQCreate, utcnow
from app.repositories.seed import DEMO_MANDATE_ID, seed_repository
from app.repositories.sqlite import SQLiteRepository


def headphone_rfq(max_pence: int = 17_000, quantity: int = 1,
                  category: str = "audio") -> RFQCreate:
    return RFQCreate(query="noise cancelling headphones", category=category,
                     max_price_pence=max_pence, quantity=quantity,
                     deadline=utcnow() + timedelta(hours=2))


@pytest.fixture
async def repository(tmp_path):
    repo = await SQLiteRepository.connect(tmp_path / "test.db")
    await seed_repository(repo)
    yield repo
    await repo.close()


@pytest.fixture
async def seeded_request(repository):
    return await repository.create_request(headphone_rfq(), mandate_id=DEMO_MANDATE_ID)
