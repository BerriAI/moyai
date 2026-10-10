"""Save the pixels inspected by view_image, not a link to a mutable source."""
import io
import base64
import os
from pathlib import Path
import stat
import uuid

WORKSPACE = Path('/workspace')
LIMIT = 2 * 1024 * 1024


def snapshot(value):
    unavailable = {'image_notice': 'Image preview could not be saved (unsupported image, path or size).'}
    try:
        from PIL import Image
    except ImportError:
        return unavailable
    try:
        path = Path(value)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.resolve().is_relative_to(WORKSPACE.resolve()):
            return unavailable
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                return unavailable
            raw = source.read(LIMIT + 1)
        if len(raw) > LIMIT:
            return unavailable
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 16_000_000:
                return unavailable
            output = io.BytesIO()
            image.convert('RGBA').save(output, format='PNG')
            preview = image.convert('RGB')
            preview.thumbnail((640, 480))
            thumbnail = io.BytesIO()
            preview.save(thumbnail, format='JPEG', quality=65)
            if thumbnail.tell() > 48 * 1024:
                preview.thumbnail((320, 240))
                thumbnail = io.BytesIO()
                preview.save(thumbnail, format='JPEG', quality=55)
            if thumbnail.tell() > 48 * 1024:
                return unavailable
        raw = output.getvalue()
        if len(raw) > LIMIT:
            return unavailable
        directory = WORKSPACE / 'moyai-tool-images'
        if directory.is_symlink():
            return unavailable
        directory.mkdir(exist_ok=True)
        # Keep tool previews within a bounded part of the workspace archive.
        if sum(p.stat().st_size for p in directory.iterdir()) + len(raw) > 4 * LIMIT:
            return unavailable
        target = directory / (uuid.uuid4().hex + '.png')
        with target.open('xb') as dest:
            dest.write(raw)
        return {'image_path': str(target),
                'image_preview': 'data:image/jpeg;base64,' + base64.b64encode(thumbnail.getvalue()).decode(),
                'image_notice': 'Preview scaled to fit. Full-size image available after workspace files are saved.'}
    except (OSError, TypeError, ValueError, Image.DecompressionBombError):
        return unavailable
