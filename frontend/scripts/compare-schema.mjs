// Fails when src/api/schema.d.ts is not what `npm run gen:api` produces from the
// backend's docs/api/openapi-v1.json, i.e. when the frontend types drift from the contract.
import { readFileSync } from "node:fs";

const committed = readFileSync("src/api/schema.d.ts", "utf8");
const fresh = readFileSync("node_modules/.cache/schema.check.d.ts", "utf8");
if (committed !== fresh) {
  console.error("src/api/schema.d.ts is out of date with docs/api/openapi-v1.json: run npm run gen:api");
  process.exit(1);
}
console.log("src/api/schema.d.ts matches docs/api/openapi-v1.json");
