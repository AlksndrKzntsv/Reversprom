"""Пакетная генерация вариантов логотипа для отбора.

Каждая серия сохраняется в logotips/<NN>_<метка>/: изображения, prompt.txt
(полный промпт и параметры) и _contact.png — лист-превью всех вариантов серии.

Примеры:
    python3 generate_logo_batch.py -n 6
    python3 generate_logo_batch.py -n 6 --model gpt-image-1 --label model_v1
    python3 generate_logo_batch.py -n 6 --extra "Знак — стилизованная буква «Р» из линий чертежа."
    python3 generate_logo_batch.py -n 6 --prompt-file my_prompt.txt
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

from generate_logo import BASE_PROMPT

ROOT = Path(__file__).resolve().parent
OUT_ROOT = ROOT / "logotips"
BASE_URL = "https://apinet.cloud/v1"

# Базовый промпт просит «несколько визуальных решений» на одном листе —
# для отбора нужен ровно один логотип на изображении.
SINGLE_LOGO = (
    "На изображении должен быть РОВНО ОДИН логотип (знак + название «РЕВЕРСПРОМ»), "
    "по центру, с большими полями. Не делать лист с несколькими вариантами, "
    "не добавлять подписи, палитры, мокапы и пояснения."
)


def _next_batch_dir(label: str) -> Path:
    OUT_ROOT.mkdir(exist_ok=True)
    nums = [int(m.group(1)) for p in OUT_ROOT.iterdir() if (m := re.match(r"(\d+)_", p.name))]
    n = max(nums, default=0) + 1
    safe = re.sub(r"[^\w-]+", "_", label).strip("_") or "batch"
    d = OUT_ROOT / f"{n:02d}_{safe}"
    d.mkdir()
    return d


def _shrink(path: Path, max_side: int = 1024) -> tuple[str, bytes]:
    """Прокси отклоняет крупные файлы (413) — уменьшаем исходник для images.edit."""
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return path.name, buf.getvalue()


def _download(url: str) -> bytes:
    # CDN некоторых моделей (Ideogram) отвечает 403 на запрос без User-Agent
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=120).read()


def _contact_sheet(files: list[Path], out: Path, cols: int = 3, thumb_w: int = 512, label_px: int = 0) -> None:
    if not files:
        return
    box_h = thumb_w * 2 // 3
    thumbs = []
    for f in files:
        im = Image.open(f).convert("RGB")
        im.thumbnail((thumb_w, box_h))
        thumbs.append((f.stem, im))
    cols = min(cols, len(thumbs))
    rows = (len(thumbs) + cols - 1) // cols
    font = ImageFont.load_default(size=label_px) if label_px else None
    cell_h = box_h + (label_px + 16 if label_px else 30)
    sheet = Image.new("RGB", (cols * (thumb_w + 10) + 10, rows * cell_h + 10), (200, 200, 200))
    draw = ImageDraw.Draw(sheet)
    for i, (name, t) in enumerate(thumbs):
        x = 10 + (i % cols) * (thumb_w + 10)
        y = 10 + (i // cols) * cell_h
        sheet.paste(t, (x + (thumb_w - t.width) // 2, y + (box_h - t.height) // 2))
        draw.text((x, y + box_h + 6), name, fill=(0, 0, 0), font=font)
    sheet.save(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--count", type=int, default=6, help="сколько вариантов сгенерировать")
    ap.add_argument("--model", default="gpt-image-2.5-sunburst")
    ap.add_argument("--size", default="1536x1024", help="1536x1024, 1024x1024, 1024x1536")
    ap.add_argument("--quality", default="high", help="low, medium, high")
    ap.add_argument("--background", action="append", default=[],
                    help="описание фона; можно указать несколько — чередуются по вариантам (по умолчанию белый)")
    ap.add_argument("--prompt-file", type=Path, help="заменить базовый промпт содержимым файла")
    ap.add_argument("--extra", default="", help="дополнительные указания в конец промпта")
    ap.add_argument("--direction", action="append", default=[],
                    help="творческое направление; можно указать несколько — распределяются по вариантам по кругу")
    ap.add_argument("--input", action="append", type=Path, default=[],
                    help="доработать существующее изображение (images.edit); можно несколько — чередуются по вариантам")
    ap.add_argument("--reference", action="append", type=Path, default=[],
                    help="образец-референс (images.edit); все образцы передаются в каждый запрос")
    ap.add_argument("--label", default="", help="метка серии в имени папки")
    ap.add_argument("--workers", type=int, default=3, help="параллельных запросов")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY не найден в .env")

    base = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else BASE_PROMPT
    parts = [base, "Дополнительно:", SINGLE_LOGO]
    if args.extra:
        parts.append(args.extra)
    prompt = "\n\n".join(parts) + "\n"
    directions = args.direction or [""]
    backgrounds = args.background or ["белый (#FFFFFF)"]

    def prompt_for(i: int) -> str:
        d = directions[(i - 1) % len(directions)]
        bg = backgrounds[(i - 1) % len(backgrounds)]
        return prompt + f"\nФон: {bg}.\n" + (f"\nНаправление для этого варианта:\n{d}\n" if d else "")

    out_dir = _next_batch_dir(args.label or args.model)
    (out_dir / "prompt.txt").write_text(
        f"model: {args.model}\nsize: {args.size}\nquality: {args.quality}\ncount: {args.count}\n\n{prompt}"
        + "".join(f"\nИсходник {k}: {f}" for k, f in enumerate(args.input, 1))
        + "".join(f"\nРеференс {k}: {f}" for k, f in enumerate(args.reference, 1))
        + "".join(f"\nФон {k}: {b}" for k, b in enumerate(args.background, 1))
        + "".join(f"\nНаправление {k}: {d}" for k, d in enumerate(args.direction, 1)),
        encoding="utf-8",
    )
    print(f"Серия: {out_dir.relative_to(ROOT)}")

    client = OpenAI(api_key=api_key, base_url=BASE_URL)

    # quality/output_format понимают только модели gpt-image; остальным через прокси их не передаём
    extra = {"quality": args.quality, "output_format": "png"} if "gpt-image" in args.model else {}

    def gen(i: int) -> Path:
        kw = dict(model=args.model, prompt=prompt_for(i), size=args.size, **extra)
        if args.reference:
            res = client.images.edit(image=[_shrink(r, 640) for r in args.reference], **kw)
        elif args.input:
            res = client.images.edit(image=_shrink(args.input[(i - 1) % len(args.input)]), **kw)
        else:
            res = client.images.generate(**kw)
        item = res.data[0]
        data = base64.b64decode(item.b64_json) if item.b64_json else _download(item.url)
        path = out_dir / f"{out_dir.name.split('_')[0]}-{i:02d}.png"
        path.write_bytes(data)
        return path

    saved: list[Path] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(gen, i): i for i in range(1, args.count + 1)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                p = fut.result()
                saved.append(p)
                print(f"  готово: {p.name}")
            except Exception as e:  # продолжаем серию, даже если один запрос упал
                print(f"  вариант {i:02d}: ошибка — {e}")

    saved.sort()
    _contact_sheet(saved, out_dir / "_contact.png")
    print(f"Сохранено {len(saved)}/{args.count}; превью: {(out_dir / '_contact.png').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
