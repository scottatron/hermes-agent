import { createRequire } from "module"
import { resolve } from "path"

const root = resolve(import.meta.dirname, "..", "..", "..")
const require = createRequire(import.meta.url)

try {
  require.resolve("vite/package.json")
} catch {
  console.error(`Run from repo root: cd ${root} && npm ci`)
  process.exit(1)
}
