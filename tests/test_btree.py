import random

import pytest

from picosql.storage.btree import BPlusTree, BTreeError


def test_insert_search_and_order():
    tree = BPlusTree(order=4)
    for key in [5, 1, 9, 3, 7, 2, 8, 6, 4, 0]:
        tree.insert(key, f"v{key}")
    assert len(tree) == 10
    assert [k for k, _ in tree] == list(range(10))
    for key in range(10):
        assert tree.search(key) == f"v{key}"
    assert tree.search(99) is None
    assert (99 in tree) is False


def test_duplicate_key_rejected():
    tree = BPlusTree(order=4)
    tree.insert(1, "a")
    with pytest.raises(BTreeError, match="duplicate"):
        tree.insert(1, "b")


def test_range_scan_bounds_and_inclusivity():
    tree = BPlusTree(order=4)
    for key in range(0, 30, 2):  # 0,2,...,28
        tree.insert(key, key * 10)
    assert [k for k, _ in tree.range_scan(6, True, 14, True)] == [6, 8, 10, 12, 14]
    assert [k for k, _ in tree.range_scan(6, False, 14, True)] == [8, 10, 12, 14]
    assert [k for k, _ in tree.range_scan(6, True, 14, False)] == [6, 8, 10, 12]
    assert [k for k, _ in tree.range_scan(6, False, 14, False)] == [8, 10, 12]
    # open-ended bounds
    assert [k for k, _ in tree.range_scan(lo=None, hi=3, hi_inclusive=True)] == [0, 2]
    assert [k for k, _ in tree.range_scan(24, True, None, True)] == [24, 26, 28]
    # bounds in empty regions
    assert list(tree.range_scan(31, True, 99, True)) == []
    assert list(tree.range_scan(-99, True, -1, True)) == []


def test_range_scan_on_empty_tree():
    assert list(BPlusTree(order=4).range_scan()) == []


def test_delete_all_order_independent_of_insert_order():
    for source_order in ("asc", "desc", "random"):
        tree = BPlusTree(order=4)
        keys = list(range(50))
        if source_order == "desc":
            keys.reverse()
        elif source_order == "random":
            rng = random.Random(42)
            rng.shuffle(keys)
        for key in keys:
            tree.insert(key, key)
        for key in range(50):
            assert tree.delete(key) is True
        assert len(tree) == 0
        assert list(tree) == []
        assert tree.delete(1) is False


def test_delete_missing_key_returns_false():
    tree = BPlusTree(order=4)
    tree.insert(1, "a")
    assert tree.delete(2) is False
    assert tree.delete(1) is True


def test_height_grows_and_shrinks():
    tree = BPlusTree(order=3)  # tiny tree: max 5 keys/node, forced splits
    assert tree.stats()["height"] == 1
    for key in range(200):
        tree.insert(key, key)
    assert tree.stats()["height"] >= 3
    for key in range(200):
        tree.delete(key)
    assert tree.stats()["height"] == 1
    assert len(tree) == 0


def test_min_keys_invariant_after_random_deletes():
    tree = BPlusTree(order=4)
    for key in range(300):
        tree.insert(key, key)
    rng = random.Random(7)
    survivors = list(range(300))
    rng.shuffle(survivors)
    for key in survivors[:250]:
        tree.delete(key)
    # walk the tree: every non-root node keeps >= min_keys
    def check(node, is_root, lo, hi):
        if not is_root:
            assert len(node.keys) >= tree.min_keys
        for k in node.keys:
            if lo is not None:
                assert k >= lo
            if hi is not None:
                assert k < hi
        if node.leaf:
            assert node.keys == sorted(node.keys)
        else:
            assert len(node.children) == len(node.keys) + 1
            bounds = [None] + node.keys
            upper = list(node.keys) + [None]
            for i, child in enumerate(node.children):
                check(child, False, bounds[i], upper[i])

    check(tree._root, True, None, None)
    assert [k for k, _ in tree] == sorted(survivors[250:])
    assert len(tree) == 50


def test_randomized_differential_against_reference():
    """The safety net: 4000 random ops on a small-order tree, compared
    against a plain dict at every step, plus periodic full-range checks."""
    rng = random.Random(2026)
    tree = BPlusTree(order=4)
    reference: dict = {}
    for step in range(4000):
        key = rng.randint(0, 400)
        op = rng.random()
        if op < 0.55:
            value = f"v{step}"
            if key in reference:
                # duplicate insert must be rejected by the tree
                with pytest.raises(BTreeError):
                    tree.insert(key, value)
            else:
                tree.insert(key, value)
                reference[key] = value
        elif op < 0.85:
            assert tree.delete(key) == (key in reference)
            reference.pop(key, None)
        else:
            assert tree.search(key) == reference.get(key)
        if step % 200 == 0:
            assert [kv for kv in tree] == sorted(reference.items())
            lo = rng.randint(0, 400)
            hi = lo + rng.randint(0, 60)
            expected = [(k, v) for k, v in sorted(reference.items()) if lo <= k <= hi]
            assert list(tree.range_scan(lo, True, hi, True)) == expected
    assert len(tree) == len(reference)
    assert [kv for kv in tree] == sorted(reference.items())


def test_stats_shape():
    tree = BPlusTree(order=8)
    stats = tree.stats()
    assert stats == {"size": 0, "height": 1, "nodes": 1}
    for key in range(100):
        tree.insert(key, key)
    stats = tree.stats()
    assert stats["size"] == 100
    assert stats["height"] == 2
    assert stats["nodes"] > 2


def test_string_keys_work():
    tree = BPlusTree(order=4)
    for name in ["carol", "alice", "bob", "dave"]:
        tree.insert(name, name.upper())
    assert [k for k, _ in tree] == ["alice", "bob", "carol", "dave"]
    assert tree.search("bob") == "BOB"
    assert [k for k, _ in tree.range_scan("b", True, "d", False)] == ["bob", "carol"]
