async function jsonOrThrow(res) {
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  try { return JSON.parse(text); } catch { return text; }
}

export async function listExamples() {
  const res = await fetch("/api/examples");
  return jsonOrThrow(res);
}

export async function getExample(name) {
  // Encode each segment individually so `/` in nested paths stays literal
  const safe = name.split("/").map(encodeURIComponent).join("/");
  const res = await fetch(`/api/examples/${safe}`);
  return jsonOrThrow(res);
}

export async function solve(body) {
  const res = await fetch("/v1/placement/solve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return jsonOrThrow(res);
}

export async function splitAndSolve(body) {
  const res = await fetch("/v1/placement/split-and-solve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return jsonOrThrow(res);
}
