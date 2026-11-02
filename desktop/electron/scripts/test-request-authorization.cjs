const assert = require("node:assert/strict");
const { applyBackendAuthorization } = require("../dist/request-authorization.js");
const paperUrl = "http://127.0.0.1:8899/api/execution/paper/start";

const operator = { authorization: "Bearer vtpo_example_token-123" };
applyBackendAuthorization(operator, "internal-secret", paperUrl);
assert.deepEqual(operator, { Authorization: "Bearer vtpo_example_token-123" });

const untrusted = { authorization: "Bearer unrelated" };
applyBackendAuthorization(untrusted, "internal-secret", paperUrl);
assert.deepEqual(untrusted, { Authorization: "Bearer internal-secret" });

const missing = {};
applyBackendAuthorization(missing, "internal-secret", paperUrl);
assert.equal(missing.Authorization, "Bearer internal-secret");
const profileToken = `vtdp_${"a".repeat(43)}`;
const automatic = {};
applyBackendAuthorization(automatic, "internal-secret", paperUrl, profileToken);
assert.equal(automatic.Authorization, `Bearer ${profileToken}`);
const namedOperator = { Authorization: "Bearer vtpo_example_token-123" };
applyBackendAuthorization(namedOperator, "internal-secret", paperUrl, profileToken);
assert.equal(namedOperator.Authorization, "Bearer vtpo_example_token-123");
const forgedProfile = { Authorization: `Bearer vtdp_${"b".repeat(43)}` };
applyBackendAuthorization(forgedProfile, "internal-secret", paperUrl, profileToken);
assert.equal(forgedProfile.Authorization, `Bearer ${profileToken}`);
const nonPaper = {};
applyBackendAuthorization(nonPaper, "internal-secret", "http://127.0.0.1:8899/api/settings", profileToken);
assert.equal(nonPaper.Authorization, "Bearer internal-secret");
const duplicate = { authorization: "Bearer vtpo_test", Authorization: "Bearer other" };
applyBackendAuthorization(duplicate, "internal-secret", paperUrl);
assert.deepEqual(duplicate, { Authorization: "Bearer internal-secret" });
const settings = { Authorization: "Bearer vtpo_example_token-123" };
applyBackendAuthorization(settings, "internal-secret", "http://127.0.0.1:8899/api/settings");
assert.deepEqual(settings, { Authorization: "Bearer internal-secret" });
const ticket = { Authorization: "Bearer vtpo_example_token-123" };
applyBackendAuthorization(ticket, "internal-secret", "http://127.0.0.1:8899/auth/sse-ticket");
assert.deepEqual(ticket, { Authorization: "Bearer internal-secret" });
console.log("Desktop operator authorization routing passed");
