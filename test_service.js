"use strict";

const { spawnSync } = require("node:child_process");

const modules = [
  "service_contract",
  "test_load",
  "test_ingestion",
  "test_recommendation",
  "test_access",
  "test_api",
];

const result = spawnSync("python3", ["-m", "unittest", "-v", ...modules], { stdio: "inherit" });
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
