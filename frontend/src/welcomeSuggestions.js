export const DEFAULT_WELCOME_SUGGESTIONS = [
  "联网检索一个主题的最新公开信息,并整理成要点",
  "读取工作区中的文件,总结核心内容",
  "在知识库中检索资料并回答我的问题",
  "运行一个已安装的技能来完成重复任务",
];

export function buildWelcomeSuggestions(configSuggestions, installedSkills, limit = 6) {
  const base =
    Array.isArray(configSuggestions) && configSuggestions.length
      ? configSuggestions.map((item) => String(item)).filter(Boolean)
      : DEFAULT_WELCOME_SUGGESTIONS;
  const skillSuggestions = (Array.isArray(installedSkills) ? installedSkills : [])
    .filter((skill) => skill && typeof skill.name === "string" && skill.name)
    .slice(0, 2)
    .map((skill) =>
      skill.description
        ? `使用技能 ${skill.name}:${skill.description}`
        : `使用技能 ${skill.name}`
    );
  return [...base, ...skillSuggestions].slice(0, Math.max(limit, 1));
}
