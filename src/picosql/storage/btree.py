"""In-memory B+ tree: ordered index supporting point lookups and range scans.

Conventions (documented because they drive the trickiest code):

* Minimum-degree form: ``t = order``, MAX_KEYS = 2t-1, MIN_KEYS = t-1
  (non-root). A node splits when it reaches 2t keys.
* **Separator = lower bound**: for an internal node, child[i] holds keys in
  ``[keys[i-1], keys[i])``. A separator does NOT have to equal the right
  subtree's first key -- it only has to be a correct lower bound. This makes
  deletion much simpler: a separator that equals a deleted key stays a valid
  lower bound (the subtree minimum only grew).
* Leaves hold values in parallel to keys, plus a ``next`` pointer chaining
  leaves left-to-right: range scans ride the chain instead of re-descending.
* Keys must be mutually comparable and unique (this tree indexes the primary
  key, which the executor has already de-duplicated). NULL keys are never
  inserted -- a NULL primary key is simply unindexed (documented deviation).

Deletion uses the full algorithm: borrow from a sibling when possible,
otherwise merge and propagate the underflow upward; an internal root with no
keys collapses into its only child.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Any, Iterator, Optional


class BTreeError(Exception):
    """B+ tree misuse (e.g. duplicate key)."""


class _Node:
    __slots__ = ("leaf", "keys", "values", "children", "next")

    def __init__(self, leaf: bool):
        self.leaf = leaf
        self.keys: list = []
        self.values: list = []  # leaf only, parallel to keys
        self.children: list = []  # internal only, len(keys) + 1
        self.next: Optional["_Node"] = None  # leaf only


class BPlusTree:
    def __init__(self, order: int = 16):
        if order < 3:
            raise ValueError("order must be >= 3")
        self.t = order  # minimum degree
        self.max_keys = 2 * order - 1
        self.min_keys = order - 1
        self._root: _Node = _Node(leaf=True)
        self._size = 0
        self._height = 1

    # ---------------------------------------------------------------- basics

    def __len__(self) -> int:
        return self._size

    def __contains__(self, key) -> bool:
        return self.search(key) is not None

    def __iter__(self) -> Iterator:
        return self.range_scan()

    def stats(self) -> dict:
        nodes = 0

        def walk(node: _Node) -> None:
            nonlocal nodes
            nodes += 1
            if not node.leaf:
                for child in node.children:
                    walk(child)

        walk(self._root)
        return {"size": self._size, "height": self._height, "nodes": nodes}

    # ----------------------------------------------------------------- insert

    def insert(self, key, value: Any) -> None:
        path: list = []
        node = self._root
        while not node.leaf:
            i = bisect_right(node.keys, key)
            path.append((node, i))
            node = node.children[i]
        i = bisect_left(node.keys, key)
        if i < len(node.keys) and node.keys[i] == key:
            raise BTreeError(f"duplicate key {key!r}")
        node.keys.insert(i, key)
        node.values.insert(i, value)
        self._size += 1

        # split overflow up the path (leaf splits copy the separator up;
        # internal splits move it up)
        while len(node.keys) > self.max_keys:
            t = self.t
            if node.leaf:
                right = _Node(leaf=True)
                right.keys = node.keys[t:]
                right.values = node.values[t:]
                separator = right.keys[0]
                node.keys = node.keys[:t]
                node.values = node.values[:t]
                right.next = node.next
                node.next = right
            else:
                right = _Node(leaf=False)
                separator = node.keys[t]
                right.keys = node.keys[t + 1 :]
                right.children = node.children[t + 1 :]
                node.keys = node.keys[:t]
                node.children = node.children[: t + 1]

            if not path:
                new_root = _Node(leaf=False)
                new_root.keys = [separator]
                new_root.children = [node, right]
                self._root = new_root
                self._height += 1
                break
            parent, idx = path.pop()
            parent.keys.insert(idx, separator)
            parent.children.insert(idx + 1, right)
            node = parent

    # ----------------------------------------------------------------- search

    def search(self, key) -> Optional[Any]:
        node = self._root
        while not node.leaf:
            node = node.children[bisect_right(node.keys, key)]
        i = bisect_left(node.keys, key)
        if i < len(node.keys) and node.keys[i] == key:
            return node.values[i]
        return None

    def range_scan(
        self,
        lo=None,
        lo_inclusive: bool = True,
        hi=None,
        hi_inclusive: bool = True,
    ) -> Iterator:
        """Yield (key, value) in key order within [lo, hi] (bounds optional).

        lo=None starts at the smallest key; hi=None runs to the largest.
        """
        node = self._root
        while not node.leaf:
            node = node.children[0] if lo is None else node.children[bisect_right(node.keys, lo)]
        if lo is None:
            i = 0
        else:
            i = bisect_left(node.keys, lo)
            if not lo_inclusive and i < len(node.keys) and node.keys[i] == lo:
                i += 1
        while node is not None:
            while i < len(node.keys):
                key = node.keys[i]
                if hi is not None:
                    if key > hi or (key == hi and not hi_inclusive):
                        return
                yield key, node.values[i]
                i += 1
            node = node.next
            i = 0

    # ----------------------------------------------------------------- delete

    def delete(self, key) -> bool:
        """Remove a key. Returns False if the key is absent."""
        path: list = []
        node = self._root
        while not node.leaf:
            i = bisect_right(node.keys, key)
            path.append((node, i))
            node = node.children[i]
        i = bisect_left(node.keys, key)
        if i >= len(node.keys) or node.keys[i] != key:
            return False
        node.keys.pop(i)
        node.values.pop(i)
        self._size -= 1
        self._fix_underflow(node, path)
        return True

    def _fix_underflow(self, node: _Node, path: list) -> None:
        child = node
        while path:
            if len(child.keys) >= self.min_keys:
                return
            parent, idx = path.pop()
            left = parent.children[idx - 1] if idx > 0 else None
            right = parent.children[idx + 1] if idx + 1 < len(parent.children) else None
            if left is not None and len(left.keys) > self.min_keys:
                self._borrow_from_left(parent, idx, child, left)
            elif right is not None and len(right.keys) > self.min_keys:
                self._borrow_from_right(parent, idx, child, right)
            elif left is not None:
                self._merge(parent, idx - 1, left, child)
            elif right is not None:
                self._merge(parent, idx, child, right)
            else:  # pragma: no cover - a parent always has a sibling here
                return
            child = parent
        # the root may legitimately run under-full; collapse it instead
        if not self._root.leaf and len(self._root.keys) == 0:
            self._root = self._root.children[0]
            self._height -= 1

    def _borrow_from_left(self, parent: _Node, idx: int, child: _Node, left: _Node) -> None:
        sep = parent.keys[idx - 1]
        if child.leaf:
            child.keys.insert(0, left.keys.pop())
            child.values.insert(0, left.values.pop())
            parent.keys[idx - 1] = child.keys[0]  # new lower bound for child
        else:
            child.keys.insert(0, sep)
            parent.keys[idx - 1] = left.keys.pop()
            child.children.insert(0, left.children.pop())

    def _borrow_from_right(self, parent: _Node, idx: int, child: _Node, right: _Node) -> None:
        sep = parent.keys[idx]
        if child.leaf:
            child.keys.append(right.keys.pop(0))
            child.values.append(right.values.pop(0))
            parent.keys[idx] = right.keys[0]  # new lower bound for right
        else:
            child.keys.append(sep)
            parent.keys[idx] = right.keys.pop(0)
            child.children.append(right.children.pop(0))

    def _merge(self, parent: _Node, sep_idx: int, a: _Node, b: _Node) -> None:
        sep = parent.keys.pop(sep_idx)
        parent.children.pop(sep_idx + 1)
        if a.leaf:
            a.keys.extend(b.keys)
            a.values.extend(b.values)
            a.next = b.next
        else:
            a.keys.append(sep)
            a.keys.extend(b.keys)
            a.children.extend(b.children)
