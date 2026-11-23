import unittest

import tracker_daemon


class LivinParserTests(unittest.TestCase):
    def test_amount_parses_indonesian_thousands(self):
        self.assertEqual(tracker_daemon.parse_livin("Pembayaran Rp 23.350"),
                         ("Livin' Mandiri", 23350.0, "expense"))

    def test_amount_parses_indonesian_decimal(self):
        self.assertEqual(tracker_daemon.parse_livin("Pembayaran Rp 1.000,50"),
                         ("Livin' Mandiri", 1000.5, "expense"))

    def test_livin_extracts_merchant_and_category(self):
        self.assertEqual(
            tracker_daemon.extract(
                "noreply.livin@bankmandiri.co.id",
                "Pembayaran Rp 23.350 makan",
            ),
            ("livin-mandiri", "Livin' Mandiri", 23350.0, "food"),
        )

    def test_cancellation_and_unsupported_sender_are_ignored(self):
        body = "Transfer dibatalkan Rp 23.350"
        self.assertIsNone(tracker_daemon.extract("noreply.livin@bankmandiri.co.id", body))
        self.assertIsNone(tracker_daemon.extract("alerts@example.test", "Rp 23.350"))


if __name__ == "__main__":
    unittest.main()
