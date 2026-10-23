"""Real Pillow rendering tests; run with a font that includes Chinese glyphs."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "watermark_image.py"


def load_module():
    spec = importlib.util.spec_from_file_location("watermark_image_tested", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WatermarkImageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image, ImageChops

        cls.Image, cls.ImageChops = Image, ImageChops
        cls.renderer = load_module()
        candidates = [os.environ.get("WATERMARK_TEST_FONT", ""),
                      "/System/Library/Fonts/STHeiti Medium.ttc",
                      "/System/Library/Fonts/PingFang.ttc",
                      "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                      "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]
        cls.font = next((path for path in candidates if path and Path(path).is_file()), None)
        if cls.font is None:
            raise RuntimeError("Set WATERMARK_TEST_FONT to a TrueType/OpenType font with Chinese glyphs")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.index = 0

    def render(self, mode="single", text="Watermark 水印", **options):
        self.index += 1
        output = self.root / ("watermark-{}.png".format(self.index))
        metadata = self.renderer.render_watermark(
            output, mode=mode, text=text, font_path=options.pop("font_path", self.font), **options)
        self.assertEqual(metadata["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertTrue(metadata["pillow_version"])
        with self.Image.open(output) as image:
            image.load()
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.size, (1920, 1080))
            self.assertEqual(image.getextrema()[:3], ((255, 255),) * 3)
            alpha = image.getchannel("A")
            self.assertEqual(alpha.getextrema(), (0, 255))
            self.assertTrue(any(alpha.histogram()[1:255]))
            return metadata, alpha.copy()

    def test_import_without_pillow_and_missing_dependency_message(self):
        original_import = builtins.__import__

        def without_pillow(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ModuleNotFoundError("Pillow intentionally absent")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=without_pillow):
            renderer = load_module()
            with self.assertRaisesRegex(RuntimeError, "Pillow >= 9.1"):
                renderer.render_watermark(self.root / "missing.png", mode="single",
                                          text="Watermark", font_path=self.font)
        self.assertFalse((self.root / "missing.png").exists())

    def test_single_uses_visible_ink_bounds_and_fits(self):
        metadata, alpha = self.render(text="  Åg 水印  ", x=101, y=203)
        self.assertEqual(alpha.getbbox(), (101, 203, 101 + metadata["text_width"],
                                          203 + metadata["text_height"]))
        width, height = metadata["text_width"], metadata["text_height"]
        _, edge = self.render(text="  Åg 水印  ", x=1920 - width, y=1080 - height)
        self.assertEqual(edge.getbbox()[2:], (1920, 1080))
        with self.assertRaisesRegex(ValueError, "fit completely"):
            self.render(text="Watermark", x=1919, y=1079)

    def test_matrix_spacing_and_phase(self):
        metadata, alpha = self.render("matrix", text="水印 WM", x=32, y=32)
        width, height = metadata["text_width"], metadata["text_height"]
        self.assertEqual(metadata["period_x"], width + 56)
        self.assertEqual(metadata["period_y"], height + 148)
        first = alpha.crop((32, 32, 32 + width, 32 + height))
        second_x, second_y = 32 + metadata["period_x"], 32 + metadata["period_y"]
        second = alpha.crop((second_x, second_y, second_x + width, second_y + height))
        self.assertEqual(first.tobytes(), second.tobytes())
        self.assertEqual(alpha.getpixel((32 + width + 10, 32 + height // 2)), 0)
        _, shifted = self.render("matrix", text="水印 WM", x=32 + metadata["period_x"], y=32)
        self.assertEqual(alpha.tobytes(), shifted.tobytes())
        _, zero = self.render("diagonal", text="水印 WM", x=32, y=32, angle=0)
        self.assertEqual(alpha.tobytes(), zero.tobytes())

    def test_diagonal_covers_all_corners_in_both_directions(self):
        for angle in (-90, -45, -30, 30, 45, 90):
            with self.subTest(angle=angle):
                metadata, alpha = self.render("diagonal", text="水印 WM", font_size=32,
                                              gap_x=16, gap_y=16, x=0, y=0, angle=angle)
                self.assertLessEqual(metadata["work_width"], 2210)
                self.assertLessEqual(metadata["work_height"], 2210)
                for box in ((0, 0, 128, 128), (1792, 0, 1920, 128),
                            (0, 952, 128, 1080), (1792, 952, 1920, 1080)):
                    self.assertIsNotNone(alpha.crop(box).getbbox(), (angle, box))

    def test_font_size_text_and_angle_change_pixels(self):
        small, first = self.render("diagonal", text="水印 WM", font_size=32, angle=30)
        large, second = self.render("diagonal", text="水印 WM", font_size=64, angle=30)
        self.assertGreater(large["text_width"], small["text_width"])
        self.assertIsNotNone(self.ImageChops.difference(first, second).getbbox())
        _, other_text = self.render("diagonal", text="不同文字 XX", font_size=32, angle=30)
        _, other_angle = self.render("diagonal", text="水印 WM", font_size=32, angle=-30)
        self.assertIsNotNone(self.ImageChops.difference(first, other_text).getbbox())
        self.assertIsNotNone(self.ImageChops.difference(first, other_angle).getbbox())

    def test_counterclockwise_rotation_matches_oversized_reference(self):
        metadata, single = self.render(text="Watermark 水印", x=0, y=0)
        tile = single.crop((0, 0, metadata["text_width"], metadata["text_height"]))
        period_x, period_y = metadata["period_x"], metadata["period_y"]
        span = 3072
        crop_x, crop_y = (span - 1920) // 2, (span - 1080) // 2
        reference = self.Image.new("L", (span, span), 0)
        for y in range((32 + crop_y) % period_y - period_y, span, period_y):
            for x in range((32 + crop_x) % period_x - period_x, span, period_x):
                reference.paste(tile, (x, y))
        expected = reference.rotate(30, resample=self.Image.Resampling.BICUBIC).crop(
            (crop_x, crop_y, crop_x + 1920, crop_y + 1080))
        _, actual = self.render("diagonal", angle=30)
        self.assertLessEqual(self.ImageChops.difference(expected, actual).getextrema()[1], 1)

    def test_custom_font_path_and_special_characters_are_literal(self):
        from PIL import ImageFont

        font = self.root / ("中文 font ; $()." + Path(self.font).suffix.lstrip("."))
        shutil.copyfile(self.font, font)
        custom = ImageFont.truetype(str(font), 44)
        chinese, missing = custom.getmask("水"), custom.getmask("\U0010ffff")
        self.assertNotEqual((chinese.size, bytes(chinese)), (missing.size, bytes(missing)))
        text = "水印 $(touch marker) `echo x`; 'quote' \\\""
        with mock.patch("os.system", side_effect=AssertionError("shell execution")), \
                mock.patch("subprocess.run", side_effect=AssertionError("process execution")):
            metadata, _ = self.render("matrix", text=text, font_path=str(font))
        self.assertEqual(metadata["text"], text)
        self.assertEqual(metadata["font_path"], str(font))
        self.assertFalse((self.root / "marker").exists())

    def test_dense_small_text_and_long_text_keep_work_canvas_bounded(self):
        for text, font_size in (("_", 8), ("WM" * 128, 256)):
            with self.subTest(font_size=font_size):
                output = self.root / ("extreme-{}.png".format(font_size))
                metadata = self.renderer.render_watermark(
                    output, mode="diagonal", text=text, font_path=self.font,
                    font_size=font_size, gap_x=0, gap_y=0, x=1919, y=1079, angle=45)
                self.assertLessEqual(metadata["work_width"], 2210)
                self.assertLessEqual(metadata["work_height"], 2210)
                with self.Image.open(output) as image:
                    self.assertEqual(image.size, (1920, 1080))
                    self.assertIsNotNone(image.getchannel("A").getbbox())

    def test_validation(self):
        invalid = [dict(mode="unknown"), dict(text=""), dict(text="   "), dict(text="a\nb"),
                   dict(text="a\n"), dict(text="a\u2028b"), dict(text="a\x00b"), dict(text="x" * 257),
                   dict(font_size=7), dict(font_size=257), dict(font_size=True),
                   dict(gap_x=-1), dict(gap_x=1921), dict(gap_y=-1), dict(gap_y=1081),
                   dict(x=-1), dict(x=1920), dict(y=-1), dict(y=1080),
                   dict(angle=-91), dict(angle=91), dict(angle=10 ** 1000), dict(angle=float("inf")),
                   dict(angle=float("nan")), dict(font_path=str(self.root / "missing.ttf"))]
        for index, options in enumerate(invalid):
            with self.subTest(options=options):
                arguments = dict(mode="single", text="Watermark", font_path=self.font)
                arguments.update(options)
                output = self.root / ("invalid-{}.png".format(index))
                with self.assertRaises(ValueError):
                    self.renderer.render_watermark(output, **arguments)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
