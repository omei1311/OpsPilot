"""依赖完整性检查。

★ 解决的问题：**"在我机器上能跑"**

    开发时随手 `pip install xxx` 装了个包，代码跑通了，
    但忘了写进 requirements.txt。
    本地一直没问题（因为包装过了），
    直到换台机器、或者进 Docker 全新安装时才崩。

    这个脚本用静态分析找出这类遗漏：
      ① 扫描 app/ 下所有 .py，提取 import 的第三方包名
      ② 拿 requirements.txt 里声明的包名对比
      ③ 报出「代码import了但依赖清单里没有」的包

    它在本地跑一下就能发现问题，不用等到部署。

用法::

    cd backend
    python scripts/check_deps.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

# 这些是标准库或项目内部模块，不需要写进依赖清单
STDLIB = {
    "abc", "argparse", "ast", "asyncio", "base64", "collections", "contextlib",
    "copy", "csv", "dataclasses", "datetime", "decimal", "enum", "functools",
    "hashlib", "hmac", "html", "http", "importlib", "inspect", "io", "itertools",
    "json", "logging", "math", "os", "pathlib", "random", "re", "secrets",
    "shutil", "signal", "socket", "sqlite3", "string", "subprocess", "sys",
    "tempfile", "textwrap", "time", "traceback", "types", "typing", "unicodedata",
    "unittest", "urllib", "uuid", "warnings", "weakref", "zipfile", "zoneinfo",
    "collections.abc", "email", "glob", "platform", "statistics", "threading",
    # __future__ 是 Python 的内置模块（from __future__ import annotations），
    # 不是第三方包。漏加的话会误报。
    "__future__",
}

# 项目内部的顶层包，不是第三方依赖
LOCAL = {"app", "scripts", "tests", "alembic"}

# import 名 → pip 包名不一致的情况。
# pip 包名和 import 名不一样很常见，静态分析认不出来，只能手工映射。
IMPORT_TO_PACKAGE = {
    "jwt": "PyJWT",
    "dotenv": "python-dotenv",
    "pydantic_settings": "pydantic-settings",
    "sqlalchemy": "SQLAlchemy",
    "langchain_core": "langchain-core",
    "langchain_openai": "langchain-openai",
    "redis": "redis",
    "bcrypt": "bcrypt",
}


def collect_imports(root: pathlib.Path) -> dict[str, set[str]]:
    """扫描目录下所有 .py，返回 {顶层包名: {哪些文件用到}}。"""
    found: dict[str, set[str]] = {}

    for path in root.rglob("*.py"):
        # 跳过迁移脚本（它们 import 的东西比较特殊）
        if "alembic" in path.parts and "versions" in path.parts:
            continue

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"  [警告] 无法解析 {path}: {exc}")
            continue

        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # 只看绝对导入；相对导入（level > 0）是项目内部的
                if node.level == 0 and node.module:
                    names = [node.module]

            for name in names:
                top = name.split(".")[0]
                if top in STDLIB or top in LOCAL:
                    continue
                found.setdefault(top, set()).add(str(path))

    return found


def collect_declared(requirements: pathlib.Path) -> set[str]:
    """从 requirements.txt 提取声明的包名（统一小写、- 和 _ 都归一）。"""
    declared: set[str] = set()
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 去掉版本约束和环境标记
        name = re.split(r"[<>=!\[;]", line)[0].strip()
        if name:
            declared.add(name.lower().replace("_", "-"))
    return declared


def main() -> int:
    backend = pathlib.Path(__file__).resolve().parent.parent
    app_dir = backend / "app"
    scripts_dir = backend / "scripts"
    req_file = backend / "requirements.txt"

    print("=" * 60)
    print("依赖完整性检查")
    print("=" * 60)

    imports = collect_imports(app_dir)
    if scripts_dir.exists():
        imports.update(collect_imports(scripts_dir))

    declared = collect_declared(req_file)

    print(f"代码里 import 了 {len(imports)} 个第三方包")
    print(f"requirements.txt 声明了 {len(declared)} 个")
    print()

    missing: list[tuple[str, str]] = []
    for import_name in sorted(imports):
        # import 名和 pip 包名可能不一样，先转换
        pkg = IMPORT_TO_PACKAGE.get(import_name, import_name)
        normalized = pkg.lower().replace("_", "-")

        if normalized not in declared:
            # 有些包被别的包间接依赖，requirements 里不写也能装上
            # （比如 langchain-openai 会带上 openai）。
            # 但只要代码直接 import 了，就应该显式声明 —— 否则上游
            # 哪天不再依赖它，你的代码就崩了。
            missing.append((import_name, pkg))

    if not missing:
        print("✅ 没有遗漏：代码 import 的包都在 requirements.txt 里")
        return 0

    print(f"❌ 发现 {len(missing)} 个遗漏：\n")
    for import_name, pkg in missing:
        files = sorted(imports[import_name])[:3]
        print(f"  import 名 : {import_name}")
        print(f"  pip 包名  : {pkg}")
        print(f"  用在哪    : {', '.join(files)}")
        print(f"  → 请加进 requirements.txt")
        print()

    print("=" * 60)
    print("为什么要管这个：本地开发时手动 pip install 过的包，")
    print("requirements.txt 里没写也能跑。但换台机器或进 Docker")
    print("全新安装时就会缺包 —— 这就是经典的『在我机器上能跑』。")
    print("=" * 60)
    return 1


if __name__ == "__main__":
    sys.exit(main())
