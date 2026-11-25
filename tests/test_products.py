# -*- coding: utf-8 -*-
"""load_products 回归测试（v2.8 任务书第 5 节）。

全部用例纯离线、零网络、临时文件用 tmp_path，不碰真实 data/ 目录。
运行：pytest tests/test_products.py -v
"""
import json
import os
import sys

import pytest

# 将项目根加入 sys.path，保证 `import llm_service` 可用
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_service as L
from llm_service import LLMConfig, load_products


class TestLoadProductsBasics:
    """基础路径：空/不存在/非法 JSON → 空列表且不抛异常。"""

    def test_empty_path_returns_empty(self):
        assert load_products("") == []
        assert load_products(None) == []  # type: ignore[arg-type]
        assert load_products("   ") == []

    def test_missing_file_returns_empty_no_exception(self, tmp_path):
        missing = tmp_path / "no_such_file.json"
        assert load_products(str(missing)) == []

    def test_invalid_json_returns_empty(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        assert load_products(str(bad)) == []


class TestLoadProductsTopLevelFormat:
    """顶层格式兼容：对象 {products: [...]} 与 直接数组 [...]。"""

    def test_top_level_object_with_products_array(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert len(out) == 1
        assert out[0]["name"] == "A"

    def test_top_level_object_without_products_key(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"other": "value"}), encoding="utf-8")
        assert load_products(str(f)) == []

    def test_top_level_object_products_not_array(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": "not an array"}), encoding="utf-8")
        assert load_products(str(f)) == []

    def test_top_level_array(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps([{"name": "B"}, {"name": "C"}]), encoding="utf-8")
        out = load_products(str(f))
        assert len(out) == 2
        assert out[0]["name"] == "B"
        assert out[1]["name"] == "C"

    def test_top_level_neither_object_nor_array(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps("just a string"), encoding="utf-8")
        assert load_products(str(f)) == []


class TestLoadProductsItemFiltering:
    """条目过滤规则：name 缺失/为空/非字符串跳过；非对象元素跳过。"""

    def test_missing_name_skipped(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"category": "x"}, {"name": "ok"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert len(out) == 1
        assert out[0]["name"] == "ok"

    def test_empty_name_skipped(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": ""}, {"name": "ok"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert len(out) == 1
        assert out[0]["name"] == "ok"

    def test_non_string_name_skipped(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": 123}, {"name": "ok"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert len(out) == 1
        assert out[0]["name"] == "ok"

    def test_non_dict_item_in_array_skipped(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": ["not a dict", {"name": "ok"}, 42]}), encoding="utf-8")
        out = load_products(str(f))
        assert len(out) == 1
        assert out[0]["name"] == "ok"


class TestLoadProductsFieldNormalization:
    """字段清洗规则：product_id 补序号、tags 非数组变 []、price 透传、category/desc 补空串。"""

    def test_product_id_auto_sequence(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A"}, {"name": "B"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["product_id"] == "1"
        assert out[1]["product_id"] == "2"

    def test_product_id_explicit_kept(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"product_id": "C001", "name": "A"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["product_id"] == "C001"

    def test_product_id_empty_string_falls_back_to_sequence(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"product_id": "", "name": "A"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["product_id"] == "1"

    def test_tags_non_array_becomes_empty_list(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A", "tags": "not an array"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["tags"] == []

    def test_tags_array_elements_str_coerced(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A", "tags": [1, 2, "three"]}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["tags"] == ["1", "2", "three"]

    def test_price_numeric_kept(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A", "price": 199}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["price"] == 199

    def test_price_string_numeric_kept(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A", "price": "299"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["price"] == "299"

    def test_price_other_value_passthrough(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A", "price": "面议"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["price"] == "面议"

    def test_category_missing_becomes_empty_string(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["category"] == ""

    def test_desc_missing_becomes_empty_string(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["desc"] == ""

    def test_category_none_becomes_empty_string(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A", "category": None}]}), encoding="utf-8")
        out = load_products(str(f))
        assert out[0]["category"] == ""

    def test_output_has_all_six_keys(self, tmp_path):
        f = tmp_path / "products.json"
        f.write_text(json.dumps({"products": [{"name": "A"}]}), encoding="utf-8")
        out = load_products(str(f))
        assert set(out[0].keys()) >= {"product_id", "name", "category", "price", "tags", "desc"}


class TestLLMConfigProductsFileField:
    """LLMConfig.from_dict 能读取 products_file 字段。"""

    def test_from_dict_reads_products_file(self):
        cfg = LLMConfig.from_dict({"products_file": "x.json"})
        assert cfg.products_file == "x.json"

    def test_from_dict_default_empty(self):
        cfg = LLMConfig.from_dict({})
        assert cfg.products_file == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))