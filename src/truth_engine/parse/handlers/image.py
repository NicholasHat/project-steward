"""Images via Pillow (dimensions/format) + exifread (EXIF).

No captioning/OCR of image content in this increment — vision-model
captioning for diagrams/photos/whiteboards is deferred (PROJECTSPECS.md
risk #8); this handler only captures deterministic embedded metadata.
"""

from __future__ import annotations

from pathlib import Path

from truth_engine.parse.registry import register
from truth_engine.parse.types import ParsedDocument

PARSER_NAME = "Pillow+exifread"
PARSER_VERSION = "1"


@register("jpg", "jpeg", "png", "tif", "tiff", "bmp", "gif")
def parse_image(path: Path) -> ParsedDocument:
    import exifread  # lazy: pipeline extra
    from PIL import Image  # lazy: pipeline extra

    with Image.open(path) as img:
        width, height = img.size
        fmt = img.format

    embedded_metadata: dict[str, object] = {"width": width, "height": height, "format": fmt}

    with path.open("rb") as f:
        tags = exifread.process_file(f, details=False)
    exif = {tag: str(value) for tag, value in tags.items() if "Thumbnail" not in tag}
    if exif:
        embedded_metadata["exif"] = exif

    return ParsedDocument(
        raw_text=None,
        structure=None,
        embedded_metadata=embedded_metadata,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
    )
