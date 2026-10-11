"""Area edits preserve the base outside the mask and retain reference order."""

import base64
from io import BytesIO
import unittest

from PIL import Image

from src.image_edit import annotate_edit, composite_edit, load_image, png, prepare_edit


def encoded(image):
    return base64.b64encode(png(image)).decode()


class ImageEditTests(unittest.TestCase):
    def test_mask_selects_second_reference_and_preserves_outside_pixels(self):
        base = Image.new("RGBA", (8, 4), (20, 70, 130, 160))
        mask = Image.new("L", base.size)
        mask.putpixel((3, 2), 255)
        mask.putpixel((4, 2), 128)
        references = [encoded(Image.new("RGB", base.size, "red")), encoded(base)]
        selected, prepared, prompt = prepare_edit(references, 1, encoded(mask), "Use the face from <image1>.")
        self.assertEqual(selected.tobytes(), base.tobytes())
        self.assertIn("Edit <image2> as the base", prompt)
        self.assertIn("green-painted area", prompt)
        annotation = load_image(annotate_edit(selected, prepared))
        self.assertEqual(annotation.getpixel((0, 0)), base.getpixel((0, 0)))
        self.assertNotEqual(annotation.getpixel((3, 2)), base.getpixel((3, 2)))
        generated = Image.new("RGBA", base.size, "green")
        result = load_image(composite_edit(png(generated), selected, prepared, base.size))
        for y in range(4):
            for x in range(8):
                if mask.getpixel((x, y)) == 0:
                    self.assertEqual(result.getpixel((x, y)), base.getpixel((x, y)))
        self.assertEqual(result.getpixel((3, 2)), generated.getpixel((3, 2)))
        self.assertNotEqual(result.getpixel((4, 2)), base.getpixel((4, 2)))
        self.assertNotEqual(result.getpixel((4, 2)), generated.getpixel((4, 2)))
        larger = load_image(composite_edit(png(generated.resize((16, 8))), selected, prepared, (16, 8)))
        self.assertEqual(larger.getpixel((0, 0)), base.getpixel((0, 0)))

    def test_invalid_or_empty_selections_fail_before_inference(self):
        ref = encoded(Image.new("RGB", (8, 4), "red"))
        white = encoded(Image.new("L", (8, 4), 255))
        for refs, index, mask, prompt in [
            ([ref], 1, white, "edit"), ([ref] * 11, 0, white, "edit"),
            ([ref], 0, "!!!", "edit"), ([ref], 0, encoded(Image.new("L", (8, 4))), "edit"),
            ([ref], 0, encoded(Image.new("RGBA", (8, 4), (255, 255, 255, 0))), "edit"),
            ([ref], 0, white, "x" * 4000),
        ]:
            with self.subTest(index=index, count=len(refs)), self.assertRaises(ValueError):
                prepare_edit(refs, index, mask, prompt)
        with self.assertRaises(ValueError):
            composite_edit(png(Image.new("RGB", (4, 4))), Image.new("RGB", (8, 4)), Image.new("L", (8, 4)), (8, 4))

    def test_exif_orientation_matches_browser_selection(self):
        image = Image.new("RGB", (8, 4), "red")
        exif = image.getexif()
        exif[274] = 6
        data = BytesIO()
        image.save(data, format="JPEG", exif=exif)
        base, mask, _ = prepare_edit([base64.b64encode(data.getvalue()).decode()], 0,
                                    encoded(Image.new("L", (4, 8), 255)), "edit")
        self.assertEqual(base.size, (4, 8))
        self.assertEqual(mask.size, base.size)

    def test_feather_stays_inside_selection(self):
        base = Image.new('RGB', (128, 128), 'blue')
        generated = png(Image.new('RGB', base.size, 'red'))
        mask = Image.new('L', base.size)
        mask.paste(255, (24, 24, 104, 104))
        result = load_image(composite_edit(generated, base, mask, base.size, 8))
        self.assertEqual(result.getpixel((23, 64)), (0, 0, 255, 255))
        self.assertEqual(result.getpixel((64, 64)), (255, 0, 0, 255))
        self.assertTrue(0 < result.getpixel((24, 64))[0] < 255)
