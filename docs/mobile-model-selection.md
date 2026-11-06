# Selecting the mobile AI model on Railway

In `pleasing-nurturing → production → Resume-Matcher → Variables`, set:

| Variable | Value |
| --- | --- |
| `LLM_PROVIDER` | `openrouter` |
| `LLM_MODEL` | Exact model ID copied from OpenRouter, such as `publisher/model-id` |
| `LLM_API_KEY` | Your OpenRouter API key; enter it directly in Railway |

Apply the variable changes with **Deploy**. A model change needs no Draftbit
release. Model IDs are text values; Railway does not provide a model catalog.

## Selection priority

With `MOBILE_API_ENABLED=true`, explicitly supplied `LLM_PROVIDER` and nonblank
`LLM_MODEL` settings override the saved `config.json` selection. Implicit
Settings defaults never replace a saved selection. When omitted, the existing
saved configuration remains in effect. Non-mobile installations retain their
existing settings behavior.

Change both provider and model together when switching providers. A provider
switch without an explicit model is rejected rather than using a model from
the previous provider. Provider-specific saved endpoints, shared keys and
reasoning options are not reused across a provider switch; set the credentials
and any required endpoint for the new provider separately. Local providers
continue to reject the generic cloud-key fallback.

The existing mobile OpenRouter rule makes `LLM_API_KEY` take precedence over
an expired saved OpenRouter key. The runtime selection does not overwrite
`config.json`; removing the selection variables and redeploying restores the
saved selection.

## Verify before adopting a model

Use a model that supports text completion and the structured output needed by
the engine. Compare the same CV and job description for factual accuracy,
completeness, wording quality, runtime and cost. Changing the model does not
change the full-tailoring defaults, page limits or processing budget, and does
not by itself establish output quality. Ordinary deterministic tests make no
paid AI calls.

No administrative configuration routes are opened by this change. API keys
remain server-side.
