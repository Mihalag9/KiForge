import os
import unittest

from core.board import module_pad_position, module_transform, ratsnest_segments
from io_kicad import board_reader


BOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Board",
                         "KiCad lab_2_gr346_br2")


class BoardReaderTests(unittest.TestCase):
    def test_lab2_unrouted(self):
        if not os.path.isdir(BOARD_DIR):
            self.skipTest("Board dir missing")
        path = os.path.join(BOARD_DIR, "lab2_gr346_br2.brd")
        with open(path, encoding="utf-8", errors="replace") as f:
            board = board_reader.read(f.read())
        self.assertEqual(board.version, 1)
        self.assertEqual(len(board.modules), 14)
        self.assertEqual(sum(len(m.pads) for m in board.modules), 178)
        self.assertEqual(len(board.tracks), 0)
        self.assertEqual(len(board.drawings), 5)
        edge = [d for d in board.drawings if d.layer == "Edge.Cuts"]
        self.assertEqual(len(edge), 5)
        vt = [m for m in board.modules if m.name == "VT"][0]
        mx, my, ang, mirror = module_transform(vt)
        self.assertAlmostEqual(mx, 179.07, places=2)
        self.assertAlmostEqual(my, 66.04, places=2)
        # пад VT с локальными (2500, 2000) — абсолютные (185.42, 71.12) мм
        pad = vt.pads[0]
        ax, ay = module_pad_position(vt, pad)
        self.assertAlmostEqual(ax, 185.42, places=2)
        self.assertAlmostEqual(ay, 71.12, places=2)

    def test_lab2_auto_tracks_and_layer(self):
        if not os.path.isdir(BOARD_DIR):
            self.skipTest("Board dir missing")
        path = os.path.join(BOARD_DIR, "lab2_gr346_br2_auto.brd")
        with open(path, encoding="utf-8", errors="replace") as f:
            board = board_reader.read(f.read())
        self.assertEqual(len(board.tracks), 245)
        layers = {t.layer for t in board.tracks}
        self.assertIn("F.Cu", layers)
        self.assertIn("B.Cu", layers)
        # хотя бы одна конечная точка дорожки совпадает с абсолютным центром пада
        pad_points = set()
        for fp in board.modules:
            for pad in fp.pads:
                ax, ay = module_pad_position(fp, pad)
                pad_points.add((round(ax, 4), round(ay, 4)))
        found = False
        for t in board.tracks:
            if not t.is_track:
                continue
            if (round(t.x1, 4), round(t.y1, 4)) in pad_points or \
               (round(t.x2, 4), round(t.y2, 4)) in pad_points:
                found = True
                break
        self.assertTrue(found)

    def test_v2_mm_no_double_convert(self):
        # синтетическая V2: координаты в мм, не должны конвертироваться повторно
        text = '''PCBNEW-BOARD Version 2 date 01/01/2013 00:00:00\n\n$GENERAL\nencoding utf-8\nUnits mm\nLayerCount 2\nLinks 0\nNoConn 0\nDi 0 0 100 100\nNdraw 0\nNtrack 0\nNzone 0\nNmodule 0\nNnets 0\n$EndGENERAL\n\n$EQUIPOT\nNa 0 ""\nSt ~\n$EndEQUIPOT\n\n$TRACK\nPo 0 10.0 20.0 30.0 40.0 0.2\nDe 15 0 0 0 0\n$EndTRACK\n\n$EndBOARD\n'''
        board = board_reader.read(text)
        self.assertEqual(board.version, 2)
        self.assertAlmostEqual(board.tracks[0].x1, 10.0)
        self.assertAlmostEqual(board.tracks[0].y1, 20.0)
        self.assertAlmostEqual(board.tracks[0].width, 0.2)
        self.assertEqual(board.tracks[0].layer, "F.Cu")

    def test_ratsnest_behavior(self):
        if not os.path.isdir(BOARD_DIR):
            self.skipTest("Board dir missing")
        path = os.path.join(BOARD_DIR, "lab2_gr346_br2_auto.brd")
        with open(path, encoding="utf-8", errors="replace") as f:
            board = board_reader.read(f.read())
        segs = ratsnest_segments(board)
        self.assertGreater(len(segs), 0)
        # цепь из одного пада линий не даёт
        single = __import__('core.board', fromlist=['Board']).Board()
        __import__('core.board', fromlist=['ratsnest_segments'])
        from core.footprint import Footprint, Pad
        fp = Footprint(name="X"); fp.po_x = 0; fp.po_y = 0; fp.po_rotation = 0; fp.po_layer = 15
        pad = Pad(x=1, y=2, net=5)
        fp.pads.append(pad)
        single.modules = [fp]
        self.assertEqual(ratsnest_segments(single), [])


if __name__ == "__main__":
    unittest.main()
