import tempfile
import time
import unittest
from pathlib import Path

from mcp_server.semantic_cache import SemanticCache
from mcp_server.adaptive_compression import decide

class SemanticCacheTests(unittest.TestCase):
    def test_rephrased_hit_and_material_mismatch_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SemanticCache(Path(tmp) / "cache.sqlite", ttl_s=60)
            cache.put("p1", "search_files", "find authentication handlers", {"a":"1"}, {"results":["auth.py"]}, "v1")
            self.assertEqual(cache.lookup("p1", "search_files", "locate auth handler", {"a":"1"}, "v1")["cache"], "semantic-hit")
            self.assertIsNone(cache.lookup("p1", "search_files", "find payment handlers", {"a":"1"}, "v1"))
            self.assertIsNone(cache.lookup("p2", "search_files", "find authentication handlers", {"a":"1"}, "v1"))

    def test_ttl_secret_and_dependency_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SemanticCache(Path(tmp) / "cache.sqlite", ttl_s=1)
            self.assertFalse(cache.put("p", "search_files", "token=secret", {}, {}, "v"))
            cache.put("p", "search_files", "find config", {"file":"a"}, {"ok":1}, "v")
            self.assertIsNone(cache.lookup("p", "search_files", "find config", {"file":"b"}, "v"))
            time.sleep(1.05)
            self.assertIsNone(cache.lookup("p", "search_files", "find config", {"file":"a"}, "v"))

    def test_sensitive_actions_are_never_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SemanticCache(Path(tmp) / "cache.sqlite")
            self.assertFalse(cache.put("p", "edit_file", "edit config", {}, {}, "v"))

class CompressionPolicyTests(unittest.TestCase):
    def test_shadow_and_safe_activation(self):
        self.assertTrue(decide(content_class="logs", size=9000).shadow)
        self.assertEqual(decide(content_class="logs", size=9000, shadow=False).mode, "compact")
        self.assertEqual(decide(content_class="config", size=9000, shadow=False).mode, "store")
        self.assertEqual(decide(content_class="unknown", size=9000, shadow=False).mode, "off")

if __name__ == "__main__":
    unittest.main()
