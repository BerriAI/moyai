Snapshot of https://api-docs.deepseek.com/guides/search/ fetched today.


Search & Fetch
On this page
Search & Fetch

The Search API and the Fetch API give agents and RAG applications access to live web content:

Search API finds web pages for a query and returns structured results. It is powered by DeepSeek's in-house search engine.

Fetch API retrieves a single web page and returns its content as Markdown.

Both are raw JSON APIs. They use the same bearer authentication as the DeepSeek API.

The base URL is https://api.deepseek.com:

APIEndpoint
SearchPOST https://api.deepseek.com/v1/tools/search
FetchPOST https://api.deepseek.com/v1/tools/fetch

See the Search API Reference and Fetch API Reference for the complete request and response schemas.

Authentication​

Send your API key and the JSON content type in the request headers:

HeaderRequiredValue
AuthorizationYesBearer <DeepSeek API Key>
Content-TypeYesapplication/json

Search API​

Request​

curl https://api.deepseek.com/v1/tools/search \
  -H "Authorization: Bearer <DeepSeek API Key>" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "DeepSeek V4 release",
    "count": 5,
    "need_content": true
  }'

import requests

response = requests.post(
    "https://api.deepseek.com/v1/tools/search",
    headers={
        "Authorization": "Bearer <DeepSeek API Key>",
    },
    json={
        "query": "DeepSeek V4 release",
        "count": 5,
        "need_content": True,
    },
)
response.raise_for_status()

for result in response.json()["results"]:
    print(result["title"], result["url"])
    print(result["summary"])

Request fields:

FieldTypeRequiredDefaultDescription
querystringYes-The search query must contain 1 to 128 Unicode characters, cannot be all whitespace, and supports the query operators listed below.
countintegerNo10The number of results to return, between 1 and 50.
ori_questionstringNo-The user's original question that triggered the search, between 1 and 128 characters, to improve query understanding, with null treated as not provided.
localizationstringNo""An optional IP address used to localize search results.
need_contentbooleanNofalseWhether to return page content in content, which is always an empty string when false.

Unknown fields in the request body are ignored.

Query Operators​

The query is forwarded with its search syntax intact. The examples below combine operators with a search term; you can combine multiple operators in one query.

OperatorExampleDescription
site:DeepSeek site:github.comRestrict results to the given site, and to target multiple sites, separate the site: clauses with spaces and join them with OR, for example DeepSeek site:github.com OR site:huggingface.co.
-site:DeepSeek -site:example.comExclude the given site, and separate multiple sites with spaces.
-DeepSeek -advertisementExclude pages containing the term, and separate multiple terms with spaces.
-"..."DeepSeek -"release notes"Exclude pages containing the exact phrase, which can contain spaces, and separate multiple phrases with spaces.
"..."DeepSeek "exact phrase"Match an exact phrase, and separate multiple phrases with spaces.
lang:DeepSeek lang:zh,enRestrict results to the given language codes, separating multiple codes with commas, for example lang:zh,en.
after:DeepSeek after:2025 or DeepSeek after:2025-01-01Restrict results to pages published after a date.
before:DeepSeek before:2026 or DeepSeek before:2026-06-01Restrict results to pages published before a date.

For example:

{
  "query": "DeepSeek site:github.com OR site:huggingface.co lang:en after:2025",
  "count": 10
}

Response​

A successful request returns HTTP 200 with a results array. A normal search with no matches returns an empty array.

{
  "results": [
    {
      "rerank_score": 2.4,
      "title": "DeepSeek V4 model released",
      "url": "https://example.com/deepseek-v4",
      "snippet": "DeepSeek V4 is now available, with tool calling and long-context support.",
      "summary": "DeepSeek has released the V4 model. The release adds improved tool calling and long-context support for agent workflows.",
      "content": "DeepSeek V4 is now generally available...",
      "site_name": "Example News",
      "site_icon": "https://example.com/favicon.ico",
      "date": "2026-06-01T00:00:00+00:00",
      "authority": 3
    }
  ]
}

Each item in results contains:

FieldTypeDescription
rerank_scorenumberA relevance score from 0 to 3 that combines signals such as relevance, authority, and freshness, with scores of 1.5 or above considered high-quality results.
titlestringThe title of the result.
urlstringThe URL of the result.
snippetstringA short summary related to the query, typically 100 to 200 characters, intended for display in a UI.
summarystringA long summary related to the query, typically 600 to 800 characters, intended to be read by a model.
contentstringThe page content, which is empty when need_content is false or when the content is unavailable.
site_namestring or nullThe name of the site, or null when unavailable.
site_iconstring or nullThe URL of the site icon, or null when unavailable.
datestring or nullThe publication time in UTC, formatted as RFC 3339, or null when unknown.
authorityintegerThe authority tier of the page, 0 to 4 (-1 means unknown), with values of 3 or above indicating highly authoritative sources.

Fetch API​

Request​

curl https://api.deepseek.com/v1/tools/fetch \
  -H "Authorization: Bearer <DeepSeek API Key>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/article"
  }'

import requests

response = requests.post(
    "https://api.deepseek.com/v1/tools/fetch",
    headers={
        "Authorization": "Bearer <DeepSeek API Key>",
    },
    json={"url": "https://example.com/article"},
)
response.raise_for_status()

page = response.json()
print(page["title"])
print(page["content"])

Request fields:

FieldTypeRequiredDescription
urlstringYesThe page URL to fetch, which must be at most 8192 characters, an HTTP or HTTPS URL with a host, and must not contain a username or password.

Response​

{
  "title": "Example article",
  "content": "# Example article\n\nArticle content."
}

FieldTypeDescription
titlestringThe title of the page, empty when the title is unavailable.
contentstringThe fetched page content as Markdown text.

Fetching is subject to the target site's availability and access policies.

Errors​

Errors follow the OpenAI-compatible error format. The param field is always null.

{
  "error": {
    "message": "'count' must be between 1 and 50",
    "type": "invalid_request_error",
    "param": null,
    "code": "invalid_request_error"
  }
}

HTTP StatusDescription
400The request headers or body are invalid, including malformed JSON or an invalid field value.
401The API key is missing or invalid.
402The account has insufficient balance.
403The Fetch request was rejected by the security policy.
404Fetch could not retrieve usable page content, for example due to an unavailable URL, a URL blocked by site policy, an error from the target site, an unsupported content type, or a page with no extractable Markdown content.
408Reading the request body timed out.
413The request body is too large.
429The Search or Fetch QPS limit was exceeded.
500An internal server error occurred.
504A downstream request timed out.

Search and Fetch are read-only. For HTTP 408, 429, or 5xx errors, wait and retry. For HTTP 402, top up your account before retrying. Do not retry other 4xx errors without changing the request.

Billing​

Search and Fetch are billed per successful request, not by tokens or by the number of returned results:

APIUnitPrice
SearchPer 1,000 successful requests$0.75
FetchPer 1,000 successful requests$0.75

A request is billable only when it returns HTTP 200 and produces a result (Search must return a non-empty results array, and Fetch must return page content). Failed requests, empty search results, and timeouts are not billed. count only controls how many results are returned and does not affect the per-request price.

Note: when you use the DeepSeek API through DeepSeek Harness, Claude Code, or Codex, server-side tool calls already hit the Search and Fetch APIs. These calls are currently free of charge; per-request billing starts at 00:00 Beijing time on November 1, 2026.

Rate Limits​

For each account, Search and Fetch are limited independently, and traffic to one does not consume the other's quota:

APIQPS Limit
Search5
Fetch5

The QPS (requests per second) limits are calculated at the account level, regardless of which API key is used, and are separate from the model concurrency limits. Requests above the account-level QPS limit receive HTTP 429. In addition to the account-level limit, Fetch is also subject to a per-domain QPS limit, and requests above that limit also receive HTTP 429.

