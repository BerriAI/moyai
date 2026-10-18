from enum import Enum
from typing import Final


class SearchProviders(str, Enum):
    """
    Enum for search provider types.
    Separate from LlmProviders for semantic clarity.
    """

    PERPLEXITY = "perplexity"
    TAVILY = "tavily"
    PARALLEL_AI = "parallel_ai"
    EXA_AI = "exa_ai"
    BRAVE = "brave"
    GOOGLE_PSE = "google_pse"
    DATAFORSEO = "dataforseo"
    FIRECRAWL = "firecrawl"
    FASTCRW = "fastcrw"
    SEARXNG = "searxng"
    LINKUP = "linkup"
    DUCKDUCKGO = "duckduckgo"
    SEARCHAPI = "searchapi"
    SERPER = "serper"
    YOU_COM = "you_com"
    APISERPENT = "apiserpent"
    TINYFISH = "tinyfish"
    AGENTCORE = "agentcore"
    NIMBLE = "nimble"
    BING_GROUNDING = "bing_grounding"


# Create a set of all search provider values for quick lookup
SearchProvidersSet: Final = {provider.value for provider in SearchProviders}
