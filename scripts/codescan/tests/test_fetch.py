"""URL vs caminho local: detecção, nome, workdir estável."""

import os
import unittest

from codescan import state as st


class TestIsUrl(unittest.TestCase):
    def test_urls(self):
        for u in ("https://github.com/a/b", "http://x/y", "git@github.com:a/b.git",
                  "ssh://git@h/a", "https://github.com/a/b.git"):
            self.assertTrue(st.is_url(u), u)

    def test_locais(self):
        for p in ("./repo", "/home/x/repo", "C:\\code\\repo", "../a"):
            self.assertFalse(st.is_url(p), p)

    def test_url_name(self):
        self.assertEqual(st.url_name("https://github.com/acme/api.git"), "api")
        self.assertEqual(st.url_name("git@github.com:acme/api"), "api")
        self.assertEqual(st.url_name("https://x/y/z/"), "z")


class TestWorkdir(unittest.TestCase):
    def test_url_estavel_e_legivel(self):
        a = st.workdir("./store", "https://github.com/acme/api.git")
        b = st.workdir("./store", "https://github.com/acme/api.git")
        self.assertEqual(a, b)  # mesma URL → mesmo workdir
        self.assertIn("api-", os.path.basename(a))

    def test_url_e_local_diferem(self):
        u = st.workdir("./store", "https://github.com/acme/api.git")
        p = st.workdir("./store", "./api")
        self.assertNotEqual(u, p)


if __name__ == "__main__":
    unittest.main()
