"""Mask conditioning and pixel compositing for the Docker image app."""

import base64
from io import BytesIO
import warnings

from PIL import Image, ImageChops, ImageFilter, ImageOps


def load_image(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.width * source.height > 20_000_000:
                    raise ValueError("Use images smaller than 20 megapixels.")
                return ImageOps.exif_transpose(source).convert("RGBA")
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Choose a readable image.") from None


def png(image):
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def edit_prompt(prompt, base):
    return (f"Edit <image{base + 1}> as the base image. The green-painted area marks the region to change. "
            "Keep the base image's framing, positions and content outside that region. "
            "Remove the green annotation and return the complete edited base image. "
            f"Apply this instruction inside the marked region: {prompt}")


def annotate_edit(base, mask):
    # Qwen supports painted annotations. Reuse the base reference's token budget
    # instead of adding a full-resolution mask as another conditioning image.
    tint = Image.new("RGBA", base.size, (0, 255, 80, 255))
    strength = mask.point(lambda value: round(value * .45))
    return png(Image.composite(tint, base, strength))


def prepare_edit(references, base, encoded_mask, prompt):
    if not 0 <= base < len(references) or len(references) > 10:
        raise ValueError("Select a base image and use up to 10 references.")
    instruction = edit_prompt(prompt, base)
    if len(instruction) > 4000:
        raise ValueError("Shorten the prompt to leave room for the area edit instructions.")
    image = load_image(base64.b64decode(references[base], validate=True))
    mask = load_image(base64.b64decode(encoded_mask, validate=True))
    # Composite transparent mask pixels over black before reading luminance.
    background = Image.new("RGBA", mask.size, "black")
    mask = Image.alpha_composite(background, mask).convert("L")
    if not mask.getbbox():
        raise ValueError("Paint the area you want to change.")
    return image, mask.resize(image.size, Image.Resampling.BILINEAR), instruction


def composite_edit(generated, base, mask, size, feather=0):
    result = load_image(generated)
    if result.size != size:
        raise ValueError("Inference returned an unexpected image size.")
    # Resize the base once. Zero-mask pixels then remain exactly this base.
    base = base.resize(size, Image.Resampling.LANCZOS)
    mask = mask.resize(size, Image.Resampling.BILINEAR)
    if feather:
        softened = mask.filter(ImageFilter.GaussianBlur(feather))
        softened = softened.point(lambda value: round(255 * (value / 255) ** 4))
        # Clip the blend to the selection: blur must never change outside pixels.
        mask = ImageChops.multiply(mask, softened)
    return png(Image.composite(result, base, mask))
