"""来源：本项目原创。CLI 是通过终端输入命令使用程序的界面。"""

import argparse
import json
import sys

from .core import Steward
from .models import DemoModel, OllamaModel


def main():
    parser = argparse.ArgumentParser(description="个人总管 v0.1：单用户本地文档项目原型")
    parser.add_argument("--db")
    parser.add_argument("--provider", choices=["demo", "ollama", "codex"], help="codex固定使用gpt-6-astra / medium")
    parser.add_argument("--model", help="已安装的本地 Ollama 总管模型；省略则为预设演示")
    parser.add_argument("--worker-model", help="可选的本地项目负责人模型")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="创建并运行一个明确标注的演示项目")
    create = sub.add_parser("create", help="记录目标与验收约定")
    create.add_argument("goal")
    create.add_argument("--acceptance", required=True)
    create.add_argument("--max-calls", type=int, default=8)
    sub.add_parser("list")
    for command in ("run", "retry", "show", "pause", "resume", "audit", "export"):
        child = sub.add_parser(command)
        child.add_argument("id")
        if command == "export":
            child.add_argument("--directory", default="work/exports")
    review = sub.add_parser("review")
    review.add_argument("id")
    review.add_argument("--decision", choices=["accept", "reject"], required=True)
    review.add_argument("--feedback", required=True)
    memory = sub.add_parser("remember", help="用户明确保存或修正一条偏好")
    memory.add_argument("key")
    memory.add_argument("value")
    sub.add_parser("memories")
    chat = sub.add_parser("chat", help="与真实个人总管持续讨论")
    chat.add_argument("message")
    answer = sub.add_parser('answer', help='为新循环项目补充信息或提交返工要求；不自动调用模型')
    answer.add_argument('id')
    answer.add_argument('message')
    budget = sub.add_parser('budget', help='明确调整累计调用名额，不重置已用次数')
    budget.add_argument('id')
    budget.add_argument('--total', type=int, required=True)
    budget.add_argument('--global-total', type=int, required=True)
    forget = sub.add_parser("forget")
    forget.add_argument("key")
    forget.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args()
    if args.worker_model and not args.model:
        parser.error("worker-model需要同时指定model，防止混淆演示与真实模式")
    provider = args.provider or ("ollama" if args.model else "demo")
    if provider == "codex":
        if args.model or args.worker_model:
            parser.error("codex模式固定使用用户指定的gpt-6-astra / medium，无需另传模型名")
        from .personal import open_personal
        app = open_personal(args.db)
    else:
        if provider == "ollama" and not args.model:
            parser.error("ollama需要指定已安装模型名")
        if provider == "demo" and (args.model or args.worker_model):
            parser.error("演示模式不能指定真实模型")
        manager = OllamaModel(args.model) if args.model else DemoModel()
        worker = OllamaModel(args.worker_model) if args.worker_model else manager
        app = Steward(args.db or "work/steward.sqlite", manager, worker)
    try:
        if args.command == "demo":
            if provider != "demo":
                parser.error("demo仅用于预设演示，真实模型请用create和run")
            p = app.create("制作个人总管框架的首轮产品验证方案", "明确目标、步骤、风险和可检查的验收条件")
            p = app.run(p["id"])
            result = {"project": p, "export": app.export(p["id"], "work/exports")}
        elif args.command == "create":
            result = app.create(args.goal, args.acceptance, args.max_calls)
        elif args.command == "chat":
            if provider != "codex":
                parser.error("持续对话请使用--provider codex")
            result = app.chat(args.message)
        elif args.command == "list":
            result = [{k: p[k] for k in ("id", "goal", "status", "mode", "calls")} for p in app.projects()]
        elif args.command == 'answer':
            if provider != 'codex':
                parser.error('补充/返工入口仅用于codex新循环项目')
            result = app.answer_project(args.id, args.message)
        elif args.command == 'budget':
            result = app.set_budget(args.id, args.total, args.global_total)
        elif args.command == "show":
            result = app.get(args.id)
        elif args.command in ("run", "retry", "pause", "resume", "audit"):
            result = getattr(app, args.command)(args.id)
        elif args.command == "export":
            result = {"path": app.export(args.id, args.directory)}
        elif args.command == "review":
            result = app.review(args.id, args.decision == "accept", args.feedback)
        elif args.command == "remember":
            app.remember(args.key, args.value)
            result = {"saved": args.key}
        elif args.command == "forget":
            app.forget(args.key)
            result = {"forgotten": args.key, "note": "不删除历史项目内容或外部备份"}
        else:
            result = app.memories()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get("status") in ("failed", "budget_exhausted"):
            return 2
        return 0
    except (ValueError, OSError) as error:
        print(f"未完成：{error}", file=sys.stderr)
        return 2
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
