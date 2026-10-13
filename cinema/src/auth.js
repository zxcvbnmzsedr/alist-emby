// AList 的角色可能是数字或数组，开发环境与 Lua 网关使用相同规则。
export function isLibraryUser(user) {
  if (!user || user.disabled) return false;
  const roles = typeof user.role === 'number' ? [user.role] : user.role;
  return Array.isArray(roles) && roles.length > 0 && roles.every(role =>
    Number.isInteger(role) && role >= 0 && role !== 1);
}
