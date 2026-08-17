const _raw = process.env.NEXT_PUBLIC_API_URL;

/**
 * 后端 base URL。
 *
 * - 未设置 / 设置为空字符串 → 返回 ""，调用方走相对路径（同源反代）
 * - 设置为非空字符串 → 走绝对 URL（跨域调试场景）
 *
 * 注意 .env.sample 推荐 `NEXT_PUBLIC_API_URL=""`，所以"空串"是合法配置。
 */
export const API_URL: string = _raw && _raw.trim() ? _raw.trim() : '';

if (!API_URL && process.env.NODE_ENV !== 'production') {
  // 仅 dev 下提示；生产构建 tree-shake 掉
  console.info('[api] NEXT_PUBLIC_API_URL 未设置，fetch 走相对路径（同源反代）');
}
