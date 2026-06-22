"""
Bing Web Search 准确率调试脚本.

用法:
    # 单条查询测试
    python tools/bing_search_test.py "蔚来ET5 尺寸参数"

    # 交互模式 (逐条输入查询)
    python tools/bing_search_test.py -i

    # 批量测试 (从文件读取查询列表)
    python tools/bing_search_test.py -f queries.txt

    # 对比多个查询变体
    python tools/bing_search_test.py -c "蔚来ET5尺寸" "蔚来ET5 长宽高" "NIO ET5 dimensions"

    # 指定结果数量
    python tools/bing_search_test.py -n 5 "F1 2025 standings"

    # 输出JSON详情 (含所有评分中间结果)
    python tools/bing_search_test.py -j "合肥工业大学 现任校长"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# 确保能导入 backend 模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from backend.tools import (  # noqa: E402
    _apply_domain_filters,
    _assess_confidence,
    _build_search_diagnostics,
    _build_search_queries_v4,
    _detect_language,
    _detect_query_type_v2,
    _evaluate_results,
    _extract_domain,
    _extract_keywords_for_query_v4,
    _infer_preferred_domains_v3,
    _merge_evaluated_results,
    _need_webfetch,
    _resolve_expected_year,
    _score_credibility,
    _score_relevance,
    _search_with_bing,
    _select_candidate_results,
    _summarize_search_results,
    web_search,
)

# 修复 Windows 终端 GBK 编码问题: 将 stdout 包装为 UTF-8 容错输出
if sys.platform == "win32":
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", buffering=1)  # type: ignore[assignment]


def _safe(text: str, max_len: int = 0) -> str:
    """截断字符串并替换不可打印字符, 避免终端编码崩溃."""
    cleaned = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "..."
    return cleaned


# --- 格式化输出 ---

SEP = "=" * 70
SEP2 = "-" * 70


def _red(text: str) -> str:
    return f"\033[91m{text}\033[0m"


def _green(text: str) -> str:
    return f"\033[92m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[93m{text}\033[0m"


def _cyan(text: str) -> str:
    return f"\033[96m{text}\033[0m"


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


def print_raw_results(results: list[dict[str, str]], title: str = "Bing 原始结果") -> None:
    """打印 Bing 返回的原始解析结果."""
    print(f"\n{_bold(title)} ({len(results)} 条)")
    print(SEP2)
    if not results:
        print(f"  {_red('(无结果)')}")
        return
    for i, item in enumerate(results):
        domain = _safe(_extract_domain(item.get("url", "")))
        print(f"  [{i+1}] {_cyan(domain)}")
        print(f"      标题: {_safe(item.get('title', ''), 100)}")
        print(f"      URL:   {_safe(item.get('url', ''), 100)}")
        print(f"      摘要: {_safe(item.get('snippet', ''), 150)}")
        print()


def print_scored_results(results: list[dict[str, Any]], title: str = "评分结果") -> None:
    """打印评分后的结果, 含 credibility / relevance / timeliness."""
    print(f"\n{_bold(title)} ({len(results)} 条)")
    print(SEP2)
    if not results:
        print(f"  {_red('(无结果)')}")
        return
    for i, item in enumerate(results):
        domain = _extract_domain(item.get("url", ""))
        cred = item.get("credibility_score", 0)
        tier = item.get("credibility_tier", "?")
        rel = item.get("relevance_score", 0)
        timely = item.get("timeliness", "?")
        preview_len = len(item.get("page_preview", "") or "")

        # 颜色标记
        cred_color = _green if cred >= 0.7 else (_yellow if cred >= 0.5 else _red)
        rel_color = _green if rel >= 3 else (_yellow if rel >= 1 else _red)

        print(f"  [{i+1}] {_cyan(domain)}")
        print(f"      可信度: {cred_color(f'{cred:.2f}')} ({tier})  |  相关性: {rel_color(f'{rel:.1f}')}  |  时效: {timely}")
        print(f"      标题: {_safe(item.get('title', ''), 100)}")
        print(f"      URL:   {_safe(item.get('url', ''), 100)}")
        if preview_len:
            print(f"      预览: {_safe(item.get('page_preview', ''), 200)}...")
        print(f"      摘要: {_safe(item.get('snippet', ''), 150)}")
        print()


def print_confidence(confidence: dict[str, Any]) -> None:
    """打印置信度评估."""
    level = confidence.get("level", "?")
    score = confidence.get("score", 0)
    reason = confidence.get("reason", "")
    sufficient = confidence.get("sufficient", False)

    if level == "high":
        color = _green
    elif level == "medium":
        color = _yellow
    else:
        color = _red

    status = _green("✓ 充分") if sufficient else _red("✗ 不足")
    print(f"\n{_bold('置信度评估')}")
    print(SEP2)
    print(f"  等级: {color(level.upper())}")
    print(f"  分数: {score:.2f}")
    print(f"  原因: {reason}")
    print(f"  充分: {status}")


def print_evaluation(evaluation: dict[str, Any] | None) -> None:
    """打印评估详情."""
    if not evaluation:
        return
    print(f"\n{_bold('评估详情')}")
    print(SEP2)
    tier_counts = evaluation.get("tier_counts", {})
    print(f"  来源层级: tier1={tier_counts.get('tier1', 0)}  tier2={tier_counts.get('tier2', 0)}  tier3={tier_counts.get('tier3', 0)}")
    print(f"  独立域名: {evaluation.get('supporting_domain_count', 0)}")
    print(f"  交叉验证: {evaluation.get('cross_validation', '?')}")
    rejected = evaluation.get("rejected_count", 0)
    if rejected:
        print(f"  被拒绝:   {rejected} 条")
        for ex in evaluation.get("rejected_examples", [])[:5]:
            print(f"    - {_extract_domain(ex.get('url', ''))}: {ex.get('reason', '?')}")


def print_query_profile(profile: dict[str, Any]) -> None:
    """打印查询分析信息."""
    print(f"\n{_bold('查询分析')}")
    print(SEP2)
    print(f"  语言:     {profile.get('language', '?')}")
    print(f"  类型:     {profile.get('query_type', '?')}")
    print(f"  期望年份: {profile.get('expected_year')}")
    print(f"  关键词:   {', '.join(profile.get('query_terms', [])[:8])}")
    print(f"  优先域名: {', '.join(profile.get('preferred_domains', [])[:5]) or '(无)'}")
    print(f"  搜索变体: {len(profile.get('search_variants', []))} 个")


def print_rejected_detail(attempts: list[dict[str, Any]]) -> None:
    """打印被拒绝结果的详细原因统计."""
    if not attempts:
        return
    print(f"\n{_bold('拒绝原因统计')}")
    print(SEP2)
    reason_counts: dict[str, int] = {}
    for attempt in attempts:
        evaluation = attempt.get("evaluation") or {}
        for ex in evaluation.get("rejected_examples", []):
            reason = ex.get("reason", "unknown")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    if not reason_counts:
        print("  (无拒绝记录)")
        return
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count} 条")


def print_search_attempts(attempts: list[dict[str, Any]]) -> None:
    """打印每次搜索尝试的概要."""
    print(f"\n{_bold('搜索尝试记录')} ({len(attempts)} 次)")
    print(SEP2)
    for i, attempt in enumerate(attempts):
        query = attempt.get("query", "")
        raw = attempt.get("raw_count", 0)
        filtered = attempt.get("filtered_count", 0)
        print(f"  [{i+1}] \"{query[:80]}\"")
        print(f"      原始: {raw} 条  →  有效: {filtered} 条")
        evaluation = attempt.get("evaluation") or {}
        rejected = evaluation.get("rejected_count", 0)
        if rejected:
            print(f"      拒绝: {rejected} 条")


def print_domain_distribution(results: list[dict[str, Any]]) -> None:
    """打印结果域名分布."""
    if not results:
        return
    print(f"\n{_bold('域名分布')}")
    print(SEP2)
    domain_counts: dict[str, int] = {}
    for item in results:
        domain = _extract_domain(item.get("url", ""))
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
    for domain, count in sorted(domain_counts.items(), key=lambda x: -x[1]):
        print(f"  {_cyan(domain)}: {count} 条")


# --- 核心测试逻辑 ---

def test_single_query(
    query: str,
    count: int = 8,
    verbose: bool = False,
    json_output: bool = False,
) -> dict[str, Any]:
    """执行单条查询的完整测试."""
    started_at = time.time()

    # 查询分析
    query_type = _detect_query_type_v2(query)
    language = _detect_language(query)
    expected_year = _resolve_expected_year(query)
    query_terms = _extract_keywords_for_query_v4(query, query_type, expected_year)
    preferred_domains = _infer_preferred_domains_v3(query, query_type)

    # 1. 原始 Bing 搜索
    raw_results = _search_with_bing(query, min(max(count, 3), 10))

    # 2. 域名过滤
    filtered_results = _apply_domain_filters(raw_results, preferred_domains or None, None)

    # 3. 评分
    scored_results, evaluation = _evaluate_results(
        query, query_type, filtered_results or raw_results,
        expected_year, query_terms, preferred_domains, count,
    )

    # 4. 候选结果
    candidates = _select_candidate_results(raw_results, count)

    # 5. 置信度
    confidence = _assess_confidence(query_type, scored_results, evaluation)

    # 6. 是否需要 fetch
    need_fetch = _need_webfetch(query_type, scored_results)

    # 7. 诊断
    insufficiencies, next_steps = _build_search_diagnostics(
        query_type, scored_results, evaluation, confidence,
    )

    duration = round(time.time() - started_at, 3)

    # --- 输出 ---
    if json_output:
        return {
            "query": query,
            "duration_seconds": duration,
            "query_profile": {
                "language": language,
                "query_type": query_type,
                "expected_year": expected_year,
                "query_terms": query_terms,
                "preferred_domains": preferred_domains,
            },
            "raw_count": len(raw_results),
            "filtered_count": len(filtered_results or []),
            "scored_count": len(scored_results),
            "results": scored_results,
            "candidates": candidates,
            "confidence": confidence,
            "evaluation": evaluation,
            "need_webfetch": need_fetch,
            "insufficiencies": insufficiencies,
            "recommended_next_steps": next_steps,
        }

    # 可读输出
    print(SEP)
    print(_bold(f"查询: {query}"))
    print(f"耗时: {duration}s")
    print(SEP)

    print_query_profile({
        "language": language,
        "query_type": query_type,
        "expected_year": expected_year,
        "query_terms": query_terms,
        "preferred_domains": preferred_domains,
        "search_variants": _build_search_queries_v4(query, query_type, language, expected_year),
    })

    if verbose:
        print_raw_results(raw_results, "Bing 原始解析结果")
        if filtered_results and filtered_results != raw_results:
            print_raw_results(filtered_results, "域名过滤后结果")

    print_scored_results(scored_results, "最终评分结果")
    print_domain_distribution(scored_results)
    print_confidence(confidence)
    print_evaluation(evaluation)

    if verbose:
        print_rejected_detail([{"evaluation": evaluation}])

    if insufficiencies:
        print(f"\n{_yellow('不足:')} {', '.join(insufficiencies)}")
    if next_steps:
        print(f"{_green('建议:')} {', '.join(next_steps)}")
    if need_fetch:
        print(f"\n{_cyan('提示:')} 建议对结果进行 web_fetch 获取完整页面内容")

    return {}


def test_compare_queries(queries: list[str], count: int = 8) -> None:
    """对比多个查询变体的结果."""
    print(SEP)
    print(_bold(f"查询对比 ({len(queries)} 个变体)"))
    print(SEP)

    all_results: list[dict[str, Any]] = []
    for i, query in enumerate(queries):
        print(f"\n{'─' * 70}")
        print(_bold(f"变体 [{i+1}/{len(queries)}]: {query}"))
        print(f"{'─' * 70}")

        query_type = _detect_query_type_v2(query)
        expected_year = _resolve_expected_year(query)
        query_terms = _extract_keywords_for_query_v4(query, query_type, expected_year)
        preferred_domains = _infer_preferred_domains_v3(query, query_type)

        raw_results = _search_with_bing(query, min(max(count, 3), 10))
        filtered_results = _apply_domain_filters(raw_results, preferred_domains or None, None)
        scored_results, evaluation = _evaluate_results(
            query, query_type, filtered_results or raw_results,
            expected_year, query_terms, preferred_domains, count,
        )
        confidence = _assess_confidence(query_type, scored_results, evaluation)

        print(f"  原始: {len(raw_results)} 条  →  有效: {len(scored_results)} 条")
        print(f"  置信度: {confidence.get('level', '?')} ({confidence.get('score', 0):.2f})")
        print(f"  类型: {query_type}  |  期望年份: {expected_year}")

        if scored_results:
            top = scored_results[0]
            print(f"  Top1: {_cyan(_extract_domain(top.get('url', '')))} | cred={top.get('credibility_score', 0):.2f} rel={top.get('relevance_score', 0):.1f}")
            print(f"        {top.get('title', '')[:100]}")

        all_results.append({
            "query": query,
            "query_type": query_type,
            "raw_count": len(raw_results),
            "scored_count": len(scored_results),
            "confidence": confidence,
            "top_domain": _extract_domain(scored_results[0]["url"]) if scored_results else "N/A",
        })

    # 汇总对比表
    print(f"\n{SEP}")
    print(_bold("对比汇总"))
    print(SEP)
    print(f"  {'查询':<40} {'类型':<16} {'原始':<6} {'有效':<6} {'置信度':<8} {'Top域名'}")
    print(f"  {'-'*40} {'-'*16} {'-'*6} {'-'*6} {'-'*8} {'-'*20}")
    for r in all_results:
        conf = r["confidence"]
        level = conf.get("level", "?")
        score = conf.get("score", 0)
        print(f"  {r['query'][:38]:<40} {r['query_type']:<16} {r['raw_count']:<6} {r['scored_count']:<6} {level:<2}({score:.2f}) {r['top_domain']}")


def run_interactive(count: int = 8, verbose: bool = False) -> None:
    """交互模式: 逐条输入查询, 实时查看结果."""
    print(_bold("Bing Web Search 调试工具 - 交互模式"))
    print("输入查询并按回车查看结果. 命令:")
    print("  /verbose  - 切换详细模式")
    print("  /count N  - 设置结果数量")
    print("  /exit     - 退出")
    print(SEP)

    while True:
        try:
            user_input = input("\n查询> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见!")
            break

        if not user_input:
            continue

        if user_input.lower() in {"/exit", "/quit", "/q"}:
            print("再见!")
            break
        elif user_input.lower() == "/verbose":
            verbose = not verbose
            print(f"详细模式: {'开' if verbose else '关'}")
            continue
        elif user_input.lower().startswith("/count"):
            try:
                count = int(user_input.split()[1])
                print(f"结果数量: {count}")
            except (IndexError, ValueError):
                print("用法: /count N")
            continue

        test_single_query(user_input, count=count, verbose=verbose)


def run_batch(filepath: str, count: int = 8, verbose: bool = False, json_output: bool = False) -> None:
    """批量测试: 从文件读取查询列表, 每行一条."""
    path = Path(filepath)
    if not path.exists():
        print(f"[错误] 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    queries = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    print(_bold(f"批量测试: {len(queries)} 条查询"))
    print(SEP)

    all_outputs: list[dict[str, Any]] = []
    for i, query in enumerate(queries):
        print(f"\n[{i+1}/{len(queries)}] ", end="", flush=True)
        result = test_single_query(query, count=count, verbose=verbose, json_output=json_output)
        if json_output and result:
            all_outputs.append(result)

    if json_output and all_outputs:
        # 汇总统计
        total_raw = sum(r["raw_count"] for r in all_outputs)
        total_scored = sum(r["scored_count"] for r in all_outputs)
        high_conf = sum(1 for r in all_outputs if r["confidence"].get("level") == "high")
        low_conf = sum(1 for r in all_outputs if r["confidence"].get("level") == "low")

        print(f"\n{SEP}")
        print(_bold("批量统计"))
        print(SEP)
        print(f"  总查询数:   {len(all_outputs)}")
        print(f"  总原始结果: {total_raw}")
        print(f"  总有效结果: {total_scored}")
        print(f"  高置信度:   {high_conf} ({high_conf/len(all_outputs)*100:.0f}%)")
        print(f"  低置信度:   {low_conf} ({low_conf/len(all_outputs)*100:.0f}%)")
        print(f"  平均原始:   {total_raw/len(all_outputs):.1f}")
        print(f"  平均有效:   {total_scored/len(all_outputs):.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bing Web Search 准确率调试工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python bing_search_test.py "蔚来ET5 尺寸"
  python bing_search_test.py -i
  python bing_search_test.py -f queries.txt
  python bing_search_test.py -c "蔚来ET5尺寸" "蔚来ET5 长宽高"
  python bing_search_test.py -j "合肥工业大学 现任校长"
        """,
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="搜索查询 (直接模式)",
    )
    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="交互模式",
    )
    parser.add_argument(
        "-f", "--file",
        type=str,
        default=None,
        help="从文件批量读取查询 (每行一条)",
    )
    parser.add_argument(
        "-c", "--compare",
        nargs="+",
        default=None,
        help="对比多个查询变体",
    )
    parser.add_argument(
        "-n", "--count",
        type=int,
        default=8,
        help="期望结果数量 (默认 8)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="详细模式: 显示原始结果和拒绝详情",
    )
    parser.add_argument(
        "-j", "--json",
        action="store_true",
        help="输出 JSON 格式 (便于后续分析)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="仅打印 Bing 原始解析结果 (跳过评分)",
    )

    args = parser.parse_args()

    # 仅原始结果模式
    if args.raw and args.query:
        raw = _search_with_bing(args.query, min(max(args.count, 3), 10))
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        return

    # JSON 输出模式
    if args.json and args.query:
        result = test_single_query(args.query, count=args.count, verbose=args.verbose, json_output=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # 交互模式
    if args.interactive:
        run_interactive(count=args.count, verbose=args.verbose)
        return

    # 批量模式
    if args.file:
        run_batch(args.file, count=args.count, verbose=args.verbose, json_output=args.json)
        return

    # 对比模式
    if args.compare:
        test_compare_queries(args.compare, count=args.count)
        return

    # 直接查询模式
    if args.query:
        test_single_query(args.query, count=args.count, verbose=args.verbose)
        return

    # 无参数: 默认进入交互模式
    print("未指定查询. 进入交互模式 (--help 查看用法)")
    run_interactive(count=args.count, verbose=args.verbose)


if __name__ == "__main__":
    main()
