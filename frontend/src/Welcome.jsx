import { BookOpen, FileText, Globe, Zap } from "lucide-react";

const CAPABILITY_CARDS = [
  { icon: Globe, title: "联网检索", description: "搜索公开网络信息并整理来源" },
  { icon: FileText, title: "文件分析", description: "读取工作区文件并回答内容问题" },
  { icon: BookOpen, title: "知识库问答", description: "基于导入的个人知识库作答" },
  { icon: Zap, title: "技能执行", description: "调用已安装技能完成固定任务" },
];

export default function Welcome({ suggestions = [], onPick }) {
  return (
    <section className="welcome" aria-label="欢迎引导">
      <div className="welcome-hero">
        <h2>今天要做点什么?</h2>
        <p>描述你的需求,Agent 会规划步骤并使用合适的工具完成。</p>
      </div>
      <div className="welcome-cards">
        {CAPABILITY_CARDS.slice(0, 4).map(({ icon: Icon, title, description }) => (
          <div className="welcome-card" key={title}>
            <div className="welcome-card-icon">
              <Icon size={16} />
            </div>
            <div className="welcome-card-title">{title}</div>
            <div className="welcome-card-desc">{description}</div>
          </div>
        ))}
      </div>
      {suggestions.length ? (
        <div className="welcome-suggestions" aria-label="建议提示词">
          {suggestions.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              className="welcome-chip"
              onClick={() => onPick?.(suggestion)}
            >
              {suggestion}
            </button>
          ))}
        </div>
      ) : null}
    </section>
  );
}
