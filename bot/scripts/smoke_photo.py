import asyncio
import json
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import MealPhotoModel
from bot.services.foodai import analyze_photo, _tg_file_url


async def main() -> None:
    async with sessionmaker() as s:
        row = await s.scalar(select(MealPhotoModel).order_by(MealPhotoModel.id.desc()))
        if not row:
            print("NO_PHOTO_ROWS")
            return
        fid = row.tg_file_id
        url = await _tg_file_url(fid)
        print("TG_URL=", url)
        res = await analyze_photo(fid)
        print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
