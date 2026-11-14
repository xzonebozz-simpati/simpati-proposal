import unittest

from backend.security import SlidingWindow, make_session, read_session

SECRET = "x" * 40


class SessionTests(unittest.TestCase):
    def test_roundtrip(self):
        token = make_session(SECRET, "dps.tambang", "v1", 3600, now=1000)
        data = read_session(SECRET, token, now=1500)
        self.assertEqual((data["u"], data["v"]), ("dps.tambang", "v1"))

    def test_expired(self):
        token = make_session(SECRET, "a", "v1", 100, now=1000)
        self.assertIsNone(read_session(SECRET, token, now=1100))
        self.assertIsNotNone(read_session(SECRET, token, now=1099))

    def test_wrong_secret_and_tamper(self):
        token = make_session(SECRET, "a", "v1", 3600, now=1000)
        self.assertIsNone(read_session("y" * 40, token, now=1001))
        body, sig = token.split(".")
        other = make_session(SECRET, "admin", "v1", 3600, now=1000).split(".")[0]
        self.assertIsNone(read_session(SECRET, other + "." + sig, now=1001))
        self.assertIsNone(read_session(SECRET, body + "." + "0" * 64, now=1001))

    def test_garbage(self):
        for bad in (None, "", "abc", "a.b.c", "." , "x" * 5000):
            self.assertIsNone(read_session(SECRET, bad, now=1))


class LimiterTests(unittest.TestCase):
    def test_lock_and_release(self):
        now = [1000.0]
        lim = SlidingWindow(3, 300, clock=lambda: now[0])
        for _ in range(3):
            self.assertEqual(lim.retry_after("ip"), 0)
            lim.hit("ip")
        self.assertGreater(lim.retry_after("ip"), 0)
        self.assertEqual(lim.retry_after("lain"), 0)
        now[0] += 301
        self.assertEqual(lim.retry_after("ip"), 0)

    def test_reset(self):
        lim = SlidingWindow(1, 300)
        lim.hit("a")
        self.assertGreater(lim.retry_after("a"), 0)
        lim.reset("a")
        self.assertEqual(lim.retry_after("a"), 0)


if __name__ == "__main__":
    unittest.main()
