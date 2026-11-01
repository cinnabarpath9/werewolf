
function healState(ask) {
  const extra = (ask && ask.extra) || {};
  if ('has_heal' in extra) return !!extra.has_heal;
  const text = String((ask && ask.text) || '').replace(/\s/g, '');
  if (/解药[:：]?已用完/.test(text)) return false;
  if (/解药[:：]?还在/.test(text)) return true;
  return null;
}
const CASES = [["engine 真实文案：解药还在", {"text": "现在是第 2 天夜晚。今晚 5号 被狼人袭击。\n你的解药：还在；毒药：还在。\n请注意：同一夜不能同时使用解药和毒药。请决定你的行动。"}, true], ["engine 真实文案：解药用完", {"text": "现在是第 3 天夜晚。今晚 5号 被狼人袭击。\n你的解药：已用完；毒药：还在。\n请注意：同一夜不能同时使用解药和毒药。请决定你的行动。"}, false], ["engine 真实文案：都用完", {"text": "现在是第 4 天夜晚。今晚没有人被袭击。\n你的解药：已用完；毒药：已用完。\n请注意：同一夜不能同时使用解药和毒药。请决定你的行动。"}, false], ["有 has_heal 字段时以字段为准（true）", {"text": "现在是第 3 天夜晚。今晚 5号 被狼人袭击。\n你的解药：已用完；毒药：还在。\n请注意：同一夜不能同时使用解药和毒药。请决定你的行动。", "extra": {"has_heal": true}}, true], ["有 has_heal 字段时以字段为准（false）", {"text": "现在是第 2 天夜晚。今晚 5号 被狼人袭击。\n你的解药：还在；毒药：还在。\n请注意：同一夜不能同时使用解药和毒药。请决定你的行动。", "extra": {"has_heal": false}}, false], ["两样都判断不出来 → null（不许瞎禁用）", {"text": "轮到你用药了。"}, null]];
let bad = 0;
for (const [name, ask, want] of CASES) {
  const got = healState(ask);
  const ok = (got === want);
  if (!ok) bad++;
  console.log((ok ? '  PASS  ' : '  FAIL  ') + name + '  → ' + JSON.stringify(got) + '（期望 ' + JSON.stringify(want) + '）');
}
console.log('\n失败 ' + bad + ' 项');
process.exit(bad ? 1 : 0);
