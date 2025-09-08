import asyncio
import json
from urllib.parse import urlparse
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
        # Security: never print full TG URL with token; show only file_path
        if url:
            try:
                path = urlparse(url).path  # e.g., /file/botTOKEN/photos/file_9.jpg
                parts = path.split('/', 3)
                file_path = parts[3] if len(parts) > 3 else path
                print("TG_FILE_PATH=", file_path)
            except Exception:
                print("TG_FILE_PATH=", "<unknown>")
        res = await analyze_photo(fid)
        print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
