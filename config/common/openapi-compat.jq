# Keeps the generated SDKs stable when the openfga/api OpenAPI document changes
# shape without changing the API. The Makefile runs this on the downloaded
# document before the per-target filters. It changes nothing in a document that
# has no skipped tags and no named request bodies.

# Operations with these tags are not generated yet. SDK users can call them
# through each SDK's API executor.
def skipped_tags: ["AuthZenService"];

def skipped: any((.tags? // [])[]; IN(skipped_tags[]));

def definition_refs:
  [.. | objects | .["$ref"]? | strings | select(startswith("#/definitions/")) | ltrimstr("#/definitions/")]
  | unique;

# The definitions reachable from the input list of definition names.
def reachable($definitions):
  def step: (. + [.[] as $name | $definitions[$name] // empty | definition_refs[]]) | unique;
  unique | until(. == step; step);

# Request bodies that the SDKs export as one model, mapped to the body they share.
def shared_bodies: {"StreamedListObjectsBody": "ListObjectsBody"};

# A schema without its docs, to compare shapes.
def shape:
  if type == "object" then
    with_entries(
      if .key == "properties" then .value |= map_values(shape)
      elif .key | IN("title", "description", "example") then empty
      else .value |= shape end)
  elif type == "array" then map(shape)
  else . end;

# 1. Drop skipped operations and the definitions that only they use.
.definitions as $definitions
| ([.paths[][] | select(skipped | not)] | definition_refs | reachable($definitions)) as $kept
| ([.paths[][] | select(skipped)] | definition_refs | reachable($definitions)) as $skipped
| .paths |= (map_values(with_entries(select(.value | skipped | not))) | with_entries(select(.value != {})))
| .definitions |= with_entries(select(.key | IN(($skipped - $kept)[]) | not))

# 2. Since openfga/api moved its openapiv2 plugin from v2.16.0 to v2.29.0, the
# document puts request bodies in definitions named <Operation>Body. Earlier
# documents inlined them, and openapi-generator named the models
# <Operation>Request. It also reused one model for identical bodies, so both
# list objects operations take ListObjectsRequest. Point each shared body at
# the body it shares, and stop if their shapes no longer match.
| reduce (shared_bodies | to_entries[]) as {key: $body, value: $shared} (.;
    if (.definitions | has($body)) and (.definitions | has($shared)) then
      if (.definitions[$body] | shape) != (.definitions[$shared] | shape) then
        error("openapi-compat: \($body) no longer matches \($shared), so the SDKs cannot share one model for both")
      else
        del(.definitions[$body])
        | walk(if type == "object" and .["$ref"]? == "#/definitions/\($body)"
               then .["$ref"] = "#/definitions/\($shared)" else . end)
      end
    else . end)

# 3. Keep the names the SDKs already export.
| ([.paths[][] | (.parameters? // [])[] | select(.in? == "body") | .schema["$ref"]? | strings
    | ltrimstr("#/definitions/") | select(endswith("Body"))] | unique) as $bodies
| reduce $bodies[] as $body (.;
    ($body | rtrimstr("Body") + "Request") as $request
    | if .definitions | has($request) then
        error("openapi-compat: cannot rename \($body) because \($request) already exists")
      else
        .definitions[$request] = .definitions[$body]
        | del(.definitions[$body])
        | walk(if type == "object" and .["$ref"]? == "#/definitions/\($body)"
               then .["$ref"] = "#/definitions/\($request)" else . end)
      end)
