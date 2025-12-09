#!/usr/bin/env python3
"""Flutter MCP Server - Real-time Flutter/Dart documentation for AI assistants"""

import asyncio
import json
import re
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime
import time

from mcp.server.fastmcp import FastMCP
import httpx
# Redis removed - using SQLite cache instead
from bs4 import BeautifulSoup
import structlog
from structlog.contextvars import bind_contextvars
from rich.console import Console

# Import our custom logging utilities
from .logging_utils import format_cache_stats, print_server_header

# Initialize structured logging
# IMPORTANT: For MCP servers, logs must go to stderr, not stdout
# stdout is reserved for the JSON-RPC protocol
import sys
import logging

# Configure structlog with enhanced formatting
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
        # Our custom processor comes before the renderer!
        format_cache_stats,
        # Use ConsoleRenderer for beautiful colored output
        structlog.dev.ConsoleRenderer(
            colors=True,
            exception_formatter=structlog.dev.plain_traceback,
        ),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()

# Rich console for direct output
console = Console(stderr=True)

# Get transport configuration from environment (needed at import time for FastMCP)
import os
_mcp_host = os.environ.get('MCP_HOST', '127.0.0.1')
_mcp_port = int(os.environ.get('MCP_PORT', '8000'))

# Initialize FastMCP server with host/port for HTTP/SSE transports
mcp = FastMCP("Flutter Docs Server", host=_mcp_host, port=_mcp_port)

# Import our SQLite-based cache
from .cache import get_cache
# Import error handling utilities
from .error_handling import (
    NetworkError, DocumentationNotFoundError, RateLimitError,
    with_retry, safe_http_get, format_error_response,
    CircuitBreaker
)
# Legacy version parser functionality now integrated in resolve_identifier()
# Import truncation utilities
from .truncation import truncate_flutter_docs, create_truncator, DocumentTruncator
# Import token management
from .token_manager import TokenManager

# Initialize cache manager
cache_manager = get_cache()
logger.info("cache_initialized", cache_type="sqlite", path=cache_manager.db_path)

# Initialize token manager
token_manager = TokenManager()


class RateLimiter:
    """Rate limiter for respectful web scraping (2 requests/second)"""
    
    def __init__(self, calls_per_second: float = 2.0):
        self.semaphore = asyncio.Semaphore(1)
        self.min_interval = 1.0 / calls_per_second
        self.last_call = 0
    
    async def acquire(self):
        async with self.semaphore:
            current_time = time.time()
            elapsed = current_time - self.last_call
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self.last_call = time.time()


# Global rate limiter instance
rate_limiter = RateLimiter()


# ============================================================================
# Helper Functions for Tool Consolidation
# ============================================================================

def resolve_identifier(identifier: str) -> Tuple[str, str, Optional[str]]:
    """
    Resolve an identifier to determine its type and clean form.
    
    Args:
        identifier: The identifier to resolve (e.g., "Container", "material.AppBar", 
                   "dart:async.Future", "provider", "dio:^5.0.0")
    
    Returns:
        Tuple of (type, clean_id, library) where:
        - type: "flutter_class", "dart_class", "pub_package", or "unknown"
        - clean_id: Cleaned identifier without prefixes or version constraints
        - library: Library name for classes, None for packages
    """
    # Check for version constraint (indicates package)
    if ':' in identifier and not identifier.startswith('dart:'):
        # It's a package with version constraint
        package_name = identifier.split(':')[0]
        return ("pub_package", package_name, None)
    
    # Check for Dart API pattern (dart:library.Class)
    if identifier.startswith('dart:'):
        match = re.match(r'dart:(\w+)\.(\w+)', identifier)
        if match:
            library = f"dart:{match.group(1)}"
            class_name = match.group(2)
            return ("dart_class", class_name, library)
        else:
            # Just dart:library without class
            return ("dart_class", identifier, None)
    
    # Check for Flutter library.class pattern
    flutter_libs = ['widgets', 'material', 'cupertino', 'painting', 'animation', 
                    'rendering', 'services', 'gestures', 'foundation']
    for lib in flutter_libs:
        if identifier.startswith(f"{lib}."):
            class_name = identifier.split('.', 1)[1]
            return ("flutter_class", class_name, lib)
    
    # Check if it's a known Flutter widget (common ones)
    common_widgets = ['Container', 'Row', 'Column', 'Text', 'Scaffold', 'AppBar',
                      'ListView', 'GridView', 'Stack', 'Card', 'IconButton']
    if identifier in common_widgets:
        return ("flutter_class", identifier, "widgets")
    
    # Check if it looks like a package name (lowercase, may contain underscores)
    if identifier.islower() or '_' in identifier:
        return ("pub_package", identifier, None)
    
    # Default to unknown
    return ("unknown", identifier, None)


def filter_by_topic(content: str, topic: str, doc_type: str) -> str:
    """
    Extract specific sections from documentation based on topic.
    
    Args:
        content: Full documentation content
        topic: Topic to filter by (e.g., "constructors", "methods", "properties", 
                "examples", "dependencies", "usage")
        doc_type: Type of documentation ("flutter_class", "dart_class", "pub_package")
    
    Returns:
        Filtered content containing only the requested topic
    """
    if not content:
        return "No content available"
    
    topic_lower = topic.lower()
    
    if doc_type in ["flutter_class", "dart_class"]:
        # For class documentation, extract specific sections
        lines = content.split('\n')
        in_section = False
        section_content = []
        section_headers = {
            "constructors": ["## Constructors", "### Constructors"],
            "methods": ["## Methods", "### Methods"],
            "properties": ["## Properties", "### Properties"],
            "examples": ["## Code Examples", "### Examples", "## Examples"],
            "description": ["## Description", "### Description"],
        }
        
        if topic_lower in section_headers:
            headers = section_headers[topic_lower]
            for i, line in enumerate(lines):
                if any(header in line for header in headers):
                    in_section = True
                    section_content.append(line)
                elif in_section and line.startswith('##'):
                    # Reached next major section
                    break
                elif in_section:
                    section_content.append(line)
            
            if section_content:
                return '\n'.join(section_content)
            else:
                return f"No {topic} section found in documentation"
        else:
            return f"Unknown topic '{topic}' for class documentation"
    
    elif doc_type == "pub_package":
        # For package documentation, different sections
        if topic_lower == "dependencies":
            # Extract dependencies from the content
            deps_match = re.search(r'"dependencies":\s*\[(.*?)\]', content, re.DOTALL)
            if deps_match:
                deps = deps_match.group(1)
                return f"Dependencies: {deps}"
            return "No dependencies information found"
        
        elif topic_lower == "usage":
            # Try to extract usage/getting started section from README
            if "readme" in content.lower():
                # Look for usage patterns in README
                patterns = [r'## Usage.*?(?=##|\Z)', r'## Getting Started.*?(?=##|\Z)',
                           r'## Quick Start.*?(?=##|\Z)', r'## Installation.*?(?=##|\Z)']
                for pattern in patterns:
                    match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
                    if match:
                        return match.group(0).strip()
            return "No usage information found"
        
        elif topic_lower == "examples":
            # Extract code examples from README
            code_blocks = re.findall(r'```(?:dart|flutter)?\n(.*?)\n```', content, re.DOTALL)
            if code_blocks:
                examples = []
                for i, code in enumerate(code_blocks[:5]):  # Limit to 5 examples
                    examples.append(f"Example {i+1}:\n```dart\n{code}\n```")
                return '\n\n'.join(examples)
            return "No code examples found"
    
    # Default: return full content if topic not recognized
    return content


def to_unified_id(doc_type: str, identifier: str, library: str = None) -> str:
    """
    Convert documentation reference to unified ID format.
    
    Args:
        doc_type: Type of documentation ("flutter_class", "dart_class", "pub_package")
        identifier: The identifier (class name or package name)
        library: Optional library name for classes
    
    Returns:
        Unified ID string (e.g., "flutter:material.AppBar", "dart:async.Future", "package:dio")
    """
    if doc_type == "flutter_class":
        if library:
            return f"flutter:{library}.{identifier}"
        else:
            return f"flutter:widgets.{identifier}"  # Default to widgets
    elif doc_type == "dart_class":
        if library:
            return f"{library}.{identifier}"
        else:
            return f"dart:core.{identifier}"  # Default to core
    elif doc_type == "pub_package":
        return f"package:{identifier}"
    else:
        return identifier


def from_unified_id(unified_id: str) -> Tuple[str, str, Optional[str]]:
    """
    Parse unified ID format back to components.
    
    Args:
        unified_id: Unified ID string (e.g., "flutter:material.AppBar")
    
    Returns:
        Tuple of (type, identifier, library)
    """
    if unified_id.startswith("flutter:"):
        parts = unified_id[8:].split('.', 1)  # Remove "flutter:" prefix
        if len(parts) == 2:
            return ("flutter_class", parts[1], parts[0])
        else:
            return ("flutter_class", parts[0], "widgets")
    
    elif unified_id.startswith("dart:"):
        match = re.match(r'(dart:\w+)\.(\w+)', unified_id)
        if match:
            return ("dart_class", match.group(2), match.group(1))
        else:
            return ("dart_class", unified_id, None)
    
    elif unified_id.startswith("package:"):
        return ("pub_package", unified_id[8:], None)
    
    else:
        return ("unknown", unified_id, None)


def estimate_doc_size(content: str) -> str:
    """
    Estimate documentation size category based on content length.
    
    Args:
        content: Documentation content
    
    Returns:
        Size category: "small", "medium", or "large"
    """
    if not content:
        return "small"
    
    # Rough token estimation (1 token ≈ 4 characters)
    estimated_tokens = len(content) / 4
    
    if estimated_tokens < 1000:
        return "small"
    elif estimated_tokens < 4000:
        return "medium"
    else:
        return "large"


def rank_results(results: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """
    Rank search results based on relevance to query.
    
    Args:
        results: List of search results
        query: Original search query
    
    Returns:
        Sorted list of results with updated relevance scores
    """
    query_lower = query.lower()
    query_words = set(query_lower.split())
    
    for result in results:
        # Start with existing relevance score if present
        score = result.get("relevance", 0.5)
        
        # Boost for exact title match
        title = result.get("title", "").lower()
        if query_lower == title:
            score += 0.5
        elif query_lower in title:
            score += 0.3
        
        # Boost for word matches in title
        title_words = set(title.split())
        word_overlap = len(query_words & title_words) / len(query_words) if query_words else 0
        score += word_overlap * 0.2
        
        # Consider description matches
        description = result.get("description", "").lower()
        if query_lower in description:
            score += 0.1
        
        # Boost for type preferences
        if "state" in query_lower and result.get("type") == "concept":
            score += 0.2
        elif "package" in query_lower and result.get("type") == "pub_package":
            score += 0.2
        elif any(word in query_lower for word in ["widget", "class"]) and result.get("type") == "flutter_class":
            score += 0.2
        
        # Cap score at 1.0
        result["relevance"] = min(score, 1.0)
    
    # Sort by relevance score (descending)
    return sorted(results, key=lambda x: x.get("relevance", 0), reverse=True)


# Circuit breakers for external services
flutter_docs_circuit = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60.0,
    expected_exception=(NetworkError, httpx.HTTPStatusError)
)

pub_dev_circuit = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60.0,
    expected_exception=(NetworkError, httpx.HTTPStatusError)
)

# Cache TTL strategy (in seconds)
CACHE_DURATIONS = {
    "flutter_api": 86400,      # 24 hours for stable APIs
    "dart_api": 86400,         # 24 hours for Dart APIs
    "pub_package": 43200,      # 12 hours for packages (may update more frequently)
    "cookbook": 604800,        # 7 days for examples
    "stackoverflow": 3600,     # 1 hour for community content
}


def get_cache_key(doc_type: str, identifier: str, version: str = None) -> str:
    """Generate cache keys for different documentation types"""
    if version:
        # Normalize version string for cache key
        version = version.replace(' ', '_').replace('>=', 'gte').replace('<=', 'lte').replace('^', 'caret')
        return f"{doc_type}:{identifier}:{version}"
    return f"{doc_type}:{identifier}"


def clean_text(element) -> str:
    """Clean and extract text from BeautifulSoup element"""
    if not element:
        return ""
    text = element.get_text(strip=True)
    # Remove excessive whitespace
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def format_constructors(constructors: List) -> str:
    """Format constructor information for AI consumption"""
    if not constructors:
        return "No constructors found"
    
    result = []
    for constructor in constructors:
        name = constructor.find('h3')
        signature = constructor.find('pre')
        desc = constructor.find('p')
        
        if name:
            result.append(f"### {clean_text(name)}")
        if signature:
            result.append(f"```dart\n{clean_text(signature)}\n```")
        if desc:
            result.append(clean_text(desc))
        result.append("")
    
    return "\n".join(result)


def format_properties(properties: List) -> str:
    """Format property information"""
    if not properties:
        return "No properties found"
    
    result = []
    for prop_list in properties:
        items = prop_list.find_all('dt')
        for item in items:
            prop_name = clean_text(item)
            prop_desc = item.find_next_sibling('dd')
            if prop_name:
                result.append(f"- **{prop_name}**: {clean_text(prop_desc) if prop_desc else 'No description'}")
    
    return "\n".join(result)


def format_methods(methods: List) -> str:
    """Format method information"""
    if not methods:
        return "No methods found"
    
    result = []
    for method in methods:
        name = method.find('h3')
        signature = method.find('pre')
        desc = method.find('p')
        
        if name:
            result.append(f"### {clean_text(name)}")
        if signature:
            result.append(f"```dart\n{clean_text(signature)}\n```")
        if desc:
            result.append(clean_text(desc))
        result.append("")
    
    return "\n".join(result)


def extract_code_examples(soup: BeautifulSoup) -> str:
    """Extract code examples from documentation"""
    examples = soup.find_all('pre', class_='language-dart')
    if not examples:
        examples = soup.find_all('pre')  # Fallback to any pre tags
    
    if not examples:
        return "No code examples found"
    
    result = []
    for i, example in enumerate(examples[:5]):  # Limit to 5 examples
        code = clean_text(example)
        if code:
            result.append(f"#### Example {i+1}:\n```dart\n{code}\n```\n")
    
    return "\n".join(result)


async def process_documentation(html: str, class_name: str, tokens: int = None) -> Dict[str, Any]:
    """Context7-style documentation processing pipeline with smart truncation and token counting.
    
    Returns a dict containing:
        - content: The processed markdown content
        - token_count: Final token count after any truncation
        - original_tokens: Original token count before truncation
        - truncated: Boolean indicating if content was truncated
        - truncation_note: Optional note about truncation
    """
    soup = BeautifulSoup(html, 'html.parser')
    
    # Remove navigation, scripts, styles, etc.
    for element in soup.find_all(['script', 'style', 'nav', 'header', 'footer']):
        element.decompose()
    
    # 1. Parse - Extract key sections
    description = soup.find('section', class_='desc')
    constructors = soup.find_all('section', class_='constructor')
    properties = soup.find_all('dl', class_='properties')
    methods = soup.find_all('section', class_='method')
    
    # 2. Enrich - Format for AI consumption
    markdown = f"""# {class_name}

## Description
{clean_text(description) if description else 'No description available'}

## Constructors
{format_constructors(constructors)}

## Properties
{format_properties(properties)}

## Methods
{format_methods(methods)}

## Code Examples
{extract_code_examples(soup)}
"""
    
    # Count tokens before truncation
    original_tokens = token_manager.count_tokens(markdown)
    truncated = False
    truncation_note = None
    
    # 3. Truncate if needed
    if tokens and original_tokens > tokens:
        markdown = truncate_flutter_docs(
            markdown,
            class_name,
            max_tokens=tokens,
            strategy="balanced"
        )
        truncated = True
        truncation_note = f"Documentation truncated from {original_tokens} to approximately {tokens} tokens"
    
    # Count final tokens
    final_tokens = token_manager.count_tokens(markdown)
    
    return {
        "content": markdown,
        "token_count": final_tokens,
        "original_tokens": original_tokens if truncated else final_tokens,
        "truncated": truncated,
        "truncation_note": truncation_note
    }


def resolve_flutter_url(query: str) -> Optional[str]:
    """Intelligently resolve documentation URLs from queries"""
    # Common Flutter class patterns
    patterns = {
        r"^(\w+)$": "https://api.flutter.dev/flutter/widgets/{0}-class.html",
        r"^widgets\.(\w+)$": "https://api.flutter.dev/flutter/widgets/{0}-class.html",
        r"^material\.(\w+)$": "https://api.flutter.dev/flutter/material/{0}-class.html",
        r"^cupertino\.(\w+)$": "https://api.flutter.dev/flutter/cupertino/{0}-class.html",
        r"^painting\.(\w+)$": "https://api.flutter.dev/flutter/painting/{0}-class.html",
        r"^animation\.(\w+)$": "https://api.flutter.dev/flutter/animation/{0}-class.html",
        r"^rendering\.(\w+)$": "https://api.flutter.dev/flutter/rendering/{0}-class.html",
        r"^services\.(\w+)$": "https://api.flutter.dev/flutter/services/{0}-class.html",
        r"^gestures\.(\w+)$": "https://api.flutter.dev/flutter/gestures/{0}-class.html",
        r"^foundation\.(\w+)$": "https://api.flutter.dev/flutter/foundation/{0}-class.html",
        # Dart core libraries
        r"^dart:core\.(\w+)$": "https://api.dart.dev/stable/dart-core/{0}-class.html",
        r"^dart:async\.(\w+)$": "https://api.dart.dev/stable/dart-async/{0}-class.html",
        r"^dart:collection\.(\w+)$": "https://api.dart.dev/stable/dart-collection/{0}-class.html",
        r"^dart:convert\.(\w+)$": "https://api.dart.dev/stable/dart-convert/{0}-class.html",
        r"^dart:io\.(\w+)$": "https://api.dart.dev/stable/dart-io/{0}-class.html",
        r"^dart:math\.(\w+)$": "https://api.dart.dev/stable/dart-math/{0}-class.html",
        r"^dart:typed_data\.(\w+)$": "https://api.dart.dev/stable/dart-typed_data/{0}-class.html",
        r"^dart:ui\.(\w+)$": "https://api.dart.dev/stable/dart-ui/{0}-class.html",
    }
    
    for pattern, url_template in patterns.items():
        if match := re.match(pattern, query, re.IGNORECASE):
            return url_template.format(*match.groups())
    
    return None




@mcp.tool()
async def get_flutter_docs(
    class_name: str, 
    library: str = "widgets",
    tokens: int = 8000
) -> Dict[str, Any]:
    """
    Get Flutter class documentation on-demand with optional smart truncation.
    
    **DEPRECATED**: This tool is deprecated. Please use flutter_docs() instead.
    The new tool provides better query resolution and unified interface.
    
    Args:
        class_name: Name of the Flutter class (e.g., "Container", "Scaffold")
        library: Flutter library (e.g., "widgets", "material", "cupertino")
        tokens: Maximum token limit for truncation (default: 8000, min: 500)
    
    Returns:
        Dictionary with documentation content or error message
    """
    bind_contextvars(tool="get_flutter_docs", class_name=class_name, library=library)
    logger.warning("deprecated_tool_usage", tool="get_flutter_docs", replacement="flutter_docs")
    
    # Validate tokens parameter
    if tokens < 500:
        return {"error": "tokens parameter must be at least 500"}
    
    # Call the new flutter_docs tool
    identifier = f"{library}.{class_name}" if library != "widgets" else class_name
    result = await flutter_docs(identifier, max_tokens=tokens)
    
    # Transform back to old format
    if result.get("error"):
        return {
            "error": result["error"],
            "suggestion": result.get("suggestion", "")
        }
    else:
        return {
            "source": result.get("source", "live"),
            "class": result.get("class", class_name),
            "library": result.get("library", library),
            "content": result.get("content", ""),
            "fetched_at": datetime.utcnow().isoformat(),
            "truncated": result.get("truncated", False)
        }


async def _get_flutter_docs_impl(
    class_name: str, 
    library: str = "widgets",
    tokens: int = None
) -> Dict[str, Any]:
    """
    Internal implementation of get_flutter_docs functionality.
    """
    # Check cache first
    cache_key = get_cache_key("flutter_api", f"{library}:{class_name}")
    
    # Check cache
    cached_data = cache_manager.get(cache_key)
    if cached_data:
        logger.info("cache_hit")
        return cached_data
    
    # Rate-limited fetch from Flutter docs
    await rate_limiter.acquire()
    
    # Determine URL based on library type
    if library.startswith("dart:"):
        # Convert dart:core to dart-core format for Dart API
        dart_lib = library.replace("dart:", "dart-")
        url = f"https://api.dart.dev/stable/{dart_lib}/{class_name}-class.html"
    else:
        # Flutter libraries use api.flutter.dev
        url = f"https://api.flutter.dev/flutter/{library}/{class_name}-class.html"
    
    logger.info("fetching_docs", url=url)
    
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "Flutter-MCP-Docs/1.0 (github.com/flutter-mcp/flutter-mcp)"
                }
            )
            response.raise_for_status()
            
            # Process HTML - Context7 style pipeline with truncation
            doc_result = await process_documentation(response.text, class_name, tokens)
            
            # Cache the result with token metadata
            result = {
                "source": "live",
                "class": class_name,
                "library": library,
                "content": doc_result["content"],
                "fetched_at": datetime.utcnow().isoformat(),
                "truncated": doc_result["truncated"],
                "token_count": doc_result["token_count"],
                "original_tokens": doc_result["original_tokens"],
                "truncation_note": doc_result["truncation_note"]
            }
            cache_manager.set(cache_key, result, CACHE_DURATIONS["flutter_api"], token_count=doc_result["token_count"])
            
            logger.info("docs_fetched_success", 
                       content_length=len(doc_result["content"]),
                       token_count=doc_result["token_count"],
                       truncated=doc_result["truncated"])
            return result
            
    except httpx.HTTPStatusError as e:
        logger.error("http_error", status_code=e.response.status_code)
        return {
            "error": f"HTTP {e.response.status_code}: Documentation not found for {library}.{class_name}",
            "suggestion": "Check the class name and library. Common libraries: widgets, material, cupertino"
        }
    except Exception as e:
        logger.error("fetch_error", error=str(e))
        return {
            "error": f"Failed to fetch documentation: {str(e)}",
            "url": url
        }


@mcp.tool()
async def search_flutter_docs(query: str, tokens: int = 5000) -> Dict[str, Any]:
    """
    Search across Flutter/Dart documentation sources with fuzzy matching.
    
    **DEPRECATED**: This tool is deprecated. Please use flutter_search() instead.
    The new tool provides better filtering and more structured results.
    
    Searches Flutter API docs, Dart API docs, and pub.dev packages.
    Returns top 5-10 most relevant results with brief descriptions.
    
    Args:
        query: Search query (e.g., "state management", "Container", "navigation", "http requests")
        tokens: Maximum token limit for response (default: 5000, min: 500)
    
    Returns:
        Search results with relevance scores and brief descriptions
    """
    bind_contextvars(tool="search_flutter_docs", query=query)
    logger.warning("deprecated_tool_usage", tool="search_flutter_docs", replacement="flutter_search")
    
    # Validate tokens parameter
    if tokens < 500:
        return {"error": "tokens parameter must be at least 500"}
    
    # Call new flutter_search tool
    result = await flutter_search(query, limit=10)
    
    # Transform back to old format
    return {
        "query": result["query"],
        "results": result["results"],
        "total": result.get("total_results", result.get("returned_results", 0)),
        "timestamp": result.get("timestamp", datetime.utcnow().isoformat()),
        "suggestions": result.get("suggestions", [])
    }


async def _search_flutter_docs_impl(
    query: str,
    limit: int = 10,
    types: List[str] = None
) -> Dict[str, Any]:
    """
    Internal implementation of search functionality.
    """
    logger.info("searching_docs")
    
    results = []
    query_lower = query.lower()
    
    # Check cache for search results
    cache_key = get_cache_key("search_results", query_lower)
    cached_data = cache_manager.get(cache_key)
    if cached_data:
        logger.info("search_cache_hit")
        return cached_data
    
    # 1. Try direct URL resolution first (exact matches)
    if url := resolve_flutter_url(query):
        logger.info("url_resolved", url=url)
        
        # Extract class name and library from URL
        if "flutter/widgets" in url:
            library = "widgets"
        elif "flutter/material" in url:
            library = "material"
        elif "flutter/cupertino" in url:
            library = "cupertino"
        else:
            library = "unknown"
        
        class_match = re.search(r'/([^/]+)-class\.html$', url)
        if class_match:
            class_name = class_match.group(1)
            doc = await _get_flutter_docs_impl(class_name, library)
            if "error" not in doc:
                results.append({
                    "type": "flutter_class",
                    "relevance": 1.0,
                    "title": f"{class_name} ({library})",
                    "description": f"Flutter {library} widget/class",
                    "url": url,
                    "content_preview": doc.get("content", "")[:200] + "..."
                })
    
    # 2. Check common Flutter widgets and classes
    common_flutter_items = [
        # State management related
        ("StatefulWidget", "widgets", "Base class for widgets that have mutable state"),
        ("StatelessWidget", "widgets", "Base class for widgets that don't require mutable state"),
        ("State", "widgets", "Logic and internal state for a StatefulWidget"),
        ("InheritedWidget", "widgets", "Base class for widgets that propagate information down the tree"),
        ("Provider", "widgets", "A widget that provides a value to its descendants"),
        ("ValueListenableBuilder", "widgets", "Rebuilds when ValueListenable changes"),
        ("NotificationListener", "widgets", "Listens for Notifications bubbling up"),
        
        # Layout widgets
        ("Container", "widgets", "A convenience widget that combines common painting, positioning, and sizing"),
        ("Row", "widgets", "Displays children in a horizontal array"),
        ("Column", "widgets", "Displays children in a vertical array"),
        ("Stack", "widgets", "Positions children relative to the box edges"),
        ("Scaffold", "material", "Basic material design visual layout structure"),
        ("Expanded", "widgets", "Expands a child to fill available space in Row/Column"),
        ("Flexible", "widgets", "Controls how a child flexes in Row/Column"),
        ("Wrap", "widgets", "Displays children in multiple runs"),
        ("Flow", "widgets", "Positions children using transformation matrices"),
        ("Table", "widgets", "Displays children in a table layout"),
        ("Align", "widgets", "Aligns a child within itself"),
        ("Center", "widgets", "Centers a child within itself"),
        ("Positioned", "widgets", "Positions a child in a Stack"),
        ("FittedBox", "widgets", "Scales and positions child within itself"),
        ("AspectRatio", "widgets", "Constrains child to specific aspect ratio"),
        ("ConstrainedBox", "widgets", "Imposes additional constraints on child"),
        ("SizedBox", "widgets", "Box with a specified size"),
        ("FractionallySizedBox", "widgets", "Sizes child to fraction of total space"),
        ("LimitedBox", "widgets", "Limits child size when unconstrained"),
        ("Offstage", "widgets", "Lays out child as if visible but paints nothing"),
        ("LayoutBuilder", "widgets", "Builds widget tree based on parent constraints"),
        
        # Navigation
        ("Navigator", "widgets", "Manages a stack of Route objects"),
        ("Route", "widgets", "An abstraction for an entry managed by a Navigator"),
        ("MaterialPageRoute", "material", "A modal route that replaces the entire screen"),
        ("NavigationBar", "material", "Material 3 navigation bar"),
        ("NavigationRail", "material", "Material navigation rail"),
        ("BottomNavigationBar", "material", "Bottom navigation bar"),
        ("Drawer", "material", "Material design drawer"),
        ("TabBar", "material", "Material design tabs"),
        ("TabBarView", "material", "Page view for TabBar"),
        ("WillPopScope", "widgets", "Intercepts back button press"),
        ("BackButton", "material", "Material design back button"),
        
        # Input widgets
        ("TextField", "material", "A material design text field"),
        ("TextFormField", "material", "A FormField that contains a TextField"),
        ("Form", "widgets", "Container for form fields"),
        ("GestureDetector", "widgets", "Detects gestures on widgets"),
        ("InkWell", "material", "Rectangular area that responds to touch with ripple"),
        ("Dismissible", "widgets", "Can be dismissed by dragging"),
        ("Draggable", "widgets", "Can be dragged to DragTarget"),
        ("LongPressDraggable", "widgets", "Draggable triggered by long press"),
        ("DragTarget", "widgets", "Receives data from Draggable"),
        ("DropdownButton", "material", "Material design dropdown button"),
        ("Slider", "material", "Material design slider"),
        ("Switch", "material", "Material design switch"),
        ("Checkbox", "material", "Material design checkbox"),
        ("Radio", "material", "Material design radio button"),
        ("DatePicker", "material", "Material design date picker"),
        ("TimePicker", "material", "Material design time picker"),
        
        # Lists & Grids
        ("ListView", "widgets", "Scrollable list of widgets"),
        ("GridView", "widgets", "Scrollable 2D array of widgets"),
        ("CustomScrollView", "widgets", "ScrollView with slivers"),
        ("SingleChildScrollView", "widgets", "Box with single scrollable child"),
        ("PageView", "widgets", "Scrollable list that works page by page"),
        ("ReorderableListView", "material", "List where items can be reordered"),
        ("RefreshIndicator", "material", "Material design pull-to-refresh"),
        
        # Common material widgets
        ("AppBar", "material", "A material design app bar"),
        ("Card", "material", "A material design card"),
        ("ListTile", "material", "A single fixed-height row for lists"),
        ("IconButton", "material", "A material design icon button"),
        ("ElevatedButton", "material", "A material design elevated button"),
        ("FloatingActionButton", "material", "A material design floating action button"),
        ("Chip", "material", "Material design chip"),
        ("ChoiceChip", "material", "Material design choice chip"),
        ("FilterChip", "material", "Material design filter chip"),
        ("ActionChip", "material", "Material design action chip"),
        ("CircularProgressIndicator", "material", "Material circular progress"),
        ("LinearProgressIndicator", "material", "Material linear progress"),
        ("SnackBar", "material", "Material design snackbar"),
        ("BottomSheet", "material", "Material design bottom sheet"),
        ("ExpansionPanel", "material", "Material expansion panel"),
        ("Stepper", "material", "Material design stepper"),
        ("DataTable", "material", "Material design data table"),
        
        # Visual Effects
        ("Opacity", "widgets", "Makes child partially transparent"),
        ("Transform", "widgets", "Applies transformation before painting"),
        ("RotatedBox", "widgets", "Rotates child by integral quarters"),
        ("ClipRect", "widgets", "Clips child to rectangle"),
        ("ClipRRect", "widgets", "Clips child to rounded rectangle"),
        ("ClipOval", "widgets", "Clips child to oval"),
        ("ClipPath", "widgets", "Clips child to path"),
        ("DecoratedBox", "widgets", "Paints decoration around child"),
        ("BackdropFilter", "widgets", "Applies filter to existing painted content"),
        
        # Animation
        ("AnimatedBuilder", "widgets", "A widget that rebuilds when animation changes"),
        ("AnimationController", "animation", "Controls an animation"),
        ("Hero", "widgets", "Marks a child for hero animations"),
        ("AnimatedContainer", "widgets", "Animated version of Container"),
        ("AnimatedOpacity", "widgets", "Animated version of Opacity"),
        ("AnimatedPositioned", "widgets", "Animated version of Positioned"),
        ("AnimatedDefaultTextStyle", "widgets", "Animated version of DefaultTextStyle"),
        ("AnimatedAlign", "widgets", "Animated version of Align"),
        ("AnimatedPadding", "widgets", "Animated version of Padding"),
        ("AnimatedSize", "widgets", "Animates its size to match child"),
        ("AnimatedCrossFade", "widgets", "Cross-fades between two children"),
        ("AnimatedSwitcher", "widgets", "Animates when switching between children"),
        
        # Async widgets
        ("FutureBuilder", "widgets", "Builds based on interaction with a Future"),
        ("StreamBuilder", "widgets", "Builds based on interaction with a Stream"),
        
        # Utility widgets
        ("MediaQuery", "widgets", "Establishes media query subtree"),
        ("Theme", "material", "Applies theme to descendant widgets"),
        ("DefaultTextStyle", "widgets", "Default text style for descendants"),
        ("Semantics", "widgets", "Annotates widget tree with semantic descriptions"),
        ("MergeSemantics", "widgets", "Merges semantics of descendants"),
        ("ExcludeSemantics", "widgets", "Drops semantics of descendants"),
    ]
    
    # Score Flutter items based on query match
    for class_name, library, description in common_flutter_items:
        relevance = calculate_relevance(query_lower, class_name.lower(), description.lower())
        if relevance > 0.3:  # Threshold for inclusion
            results.append({
                "type": "flutter_class",
                "relevance": relevance,
                "title": f"{class_name} ({library})",
                "description": description,
                "class_name": class_name,
                "library": library
            })
    
    # 3. Check common Dart core classes
    common_dart_items = [
        ("List", "dart:core", "An indexable collection of objects with a length"),
        ("Map", "dart:core", "A collection of key/value pairs"),
        ("Set", "dart:core", "A collection of objects with no duplicate elements"),
        ("String", "dart:core", "A sequence of UTF-16 code units"),
        ("Future", "dart:async", "Represents a computation that completes with a value or error"),
        ("Stream", "dart:async", "A source of asynchronous data events"),
        ("Duration", "dart:core", "A span of time"),
        ("DateTime", "dart:core", "An instant in time"),
        ("RegExp", "dart:core", "A regular expression pattern"),
        ("Iterable", "dart:core", "A collection of values that can be accessed sequentially"),
    ]
    
    for class_name, library, description in common_dart_items:
        relevance = calculate_relevance(query_lower, class_name.lower(), description.lower())
        if relevance > 0.3:
            results.append({
                "type": "dart_class",
                "relevance": relevance,
                "title": f"{class_name} ({library})",
                "description": description,
                "class_name": class_name,
                "library": library
            })
    
    # 4. Search popular pub.dev packages
    popular_packages = [
        # State Management
        ("provider", "State management library that makes it easy to connect business logic to widgets"),
        ("riverpod", "A reactive caching and data-binding framework"),
        ("bloc", "State management library implementing the BLoC design pattern"),
        ("get", "Open source state management, navigation and utilities"),
        ("mobx", "Reactive state management library"),
        ("redux", "Predictable state container"),
        ("stacked", "MVVM architecture solution"),
        ("get_it", "Service locator for dependency injection"),
        
        # Networking
        ("dio", "Powerful HTTP client for Dart with interceptors and FormData"),
        ("http", "A composable, multi-platform, Future-based API for HTTP requests"),
        ("retrofit", "Type-safe HTTP client generator"),
        ("chopper", "HTTP client with built-in JsonConverter"),
        ("graphql_flutter", "GraphQL client for Flutter"),
        ("socket_io_client", "Socket.IO client"),
        ("web_socket_channel", "WebSocket connections"),
        
        # Storage & Database
        ("shared_preferences", "Flutter plugin for reading and writing simple key-value pairs"),
        ("sqflite", "SQLite plugin for Flutter with support for iOS, Android and MacOS"),
        ("hive", "Lightweight and blazing fast key-value database written in pure Dart"),
        ("isar", "Fast cross-platform database"),
        ("objectbox", "High-performance NoSQL database"),
        ("drift", "Reactive persistence library"),
        ("floor", "SQLite abstraction with Room-like API"),
        
        # Firebase
        ("firebase_core", "Flutter plugin to use Firebase Core API"),
        ("firebase_auth", "Flutter plugin for Firebase Auth"),
        ("firebase_database", "Flutter plugin for Firebase Realtime Database"),
        ("cloud_firestore", "Flutter plugin for Cloud Firestore"),
        ("firebase_messaging", "Push notifications via FCM"),
        ("firebase_storage", "Flutter plugin for Firebase Cloud Storage"),
        ("firebase_analytics", "Flutter plugin for Google Analytics for Firebase"),
        
        # UI/UX Libraries
        ("flutter_bloc", "Flutter widgets that make it easy to implement BLoC design pattern"),
        ("animations", "Beautiful pre-built animations for Flutter"),
        ("flutter_svg", "SVG rendering and widget library for Flutter"),
        ("cached_network_image", "Flutter library to load and cache network images"),
        ("flutter_slidable", "Slidable list item actions"),
        ("shimmer", "Shimmer loading effect"),
        ("liquid_swipe", "Liquid swipe page transitions"),
        ("flutter_staggered_grid_view", "Staggered grid layouts"),
        ("carousel_slider", "Carousel widget"),
        ("photo_view", "Zoomable image widget"),
        ("flutter_spinkit", "Loading indicators collection"),
        ("lottie", "Render After Effects animations"),
        ("rive", "Interactive animations"),
        
        # Platform Integration
        ("url_launcher", "Flutter plugin for launching URLs"),
        ("path_provider", "Flutter plugin for getting commonly used locations on the filesystem"),
        ("image_picker", "Flutter plugin for selecting images from image library or camera"),
        ("connectivity_plus", "Flutter plugin for discovering network connectivity"),
        ("permission_handler", "Permission plugin for Flutter"),
        ("geolocator", "Flutter geolocation plugin for Android and iOS"),
        ("google_fonts", "Flutter package to use fonts from fonts.google.com"),
        ("flutter_local_notifications", "Local notifications"),
        ("share_plus", "Share content to other apps"),
        ("file_picker", "Native file picker"),
        ("open_file", "Open files with default apps"),
        
        # Navigation
        ("go_router", "A declarative routing package for Flutter"),
        ("auto_route", "Code generation for type-safe route navigation"),
        ("beamer", "Handle your application routing"),
        ("fluro", "Flutter routing library"),
        
        # Developer Tools
        ("logger", "Beautiful logging utility"),
        ("pretty_dio_logger", "Dio interceptor for logging"),
        ("flutter_dotenv", "Load environment variables"),
        ("device_info_plus", "Device information"),
        ("package_info_plus", "App package information"),
        ("equatable", "Simplify equality comparisons"),
        ("freezed", "Code generation for immutable classes"),
        ("json_serializable", "Automatically generate code for JSON"),
        ("build_runner", "Build system for Dart code generation"),
    ]
    
    for package_name, description in popular_packages:
        relevance = calculate_relevance(query_lower, package_name.lower(), description.lower())
        if relevance > 0.3:
            results.append({
                "type": "pub_package",
                "relevance": relevance,
                "title": f"{package_name} (pub.dev)",
                "description": description,
                "package_name": package_name
            })
    
    # 5. Concept-based search (for queries like "state management", "navigation", etc.)
    concepts = {
        "state management": [
            ("setState", "The simplest way to manage state in Flutter"),
            ("InheritedWidget", "Share data across the widget tree"),
            ("provider", "Popular state management package"),
            ("riverpod", "Improved provider with compile-time safety"),
            ("bloc", "Business Logic Component pattern"),
            ("get", "Lightweight state management solution"),
            ("mobx", "Reactive state management"),
            ("redux", "Predictable state container"),
            ("ValueNotifier", "Simple observable pattern"),
            ("ChangeNotifier", "Observable object for multiple listeners"),
        ],
        "navigation": [
            ("Navigator", "Stack-based navigation in Flutter"),
            ("go_router", "Declarative routing package"),
            ("auto_route", "Code generation for routes"),
            ("Named routes", "Navigation using route names"),
            ("Deep linking", "Handle URLs in your app"),
            ("WillPopScope", "Intercept back navigation"),
            ("NavigatorObserver", "Observe navigation events"),
            ("Hero animations", "Animate widgets between routes"),
            ("Modal routes", "Full-screen modal pages"),
            ("BottomSheet navigation", "Navigate with bottom sheets"),
        ],
        "http": [
            ("http", "Official Dart HTTP package"),
            ("dio", "Advanced HTTP client with interceptors"),
            ("retrofit", "Type-safe HTTP client generator"),
            ("chopper", "HTTP client with built-in JsonConverter"),
            ("GraphQL", "Query language for APIs"),
            ("REST API", "RESTful web services"),
            ("WebSocket", "Real-time bidirectional communication"),
            ("gRPC", "High performance RPC framework"),
        ],
        "database": [
            ("sqflite", "SQLite for Flutter"),
            ("hive", "NoSQL database for Flutter"),
            ("drift", "Reactive persistence library"),
            ("objectbox", "Fast NoSQL database"),
            ("shared_preferences", "Simple key-value storage"),
            ("isar", "Fast cross-platform database"),
            ("floor", "SQLite abstraction"),
            ("sembast", "NoSQL persistent store"),
            ("Firebase Realtime Database", "Cloud-hosted NoSQL database"),
            ("Cloud Firestore", "Scalable NoSQL cloud database"),
        ],
        "animation": [
            ("AnimationController", "Control animations"),
            ("AnimatedBuilder", "Build animations efficiently"),
            ("Hero", "Shared element transitions"),
            ("animations", "Pre-built animation package"),
            ("rive", "Interactive animations"),
            ("lottie", "After Effects animations"),
            ("AnimatedContainer", "Implicit animations"),
            ("TweenAnimationBuilder", "Simple custom animations"),
            ("Curves", "Animation easing functions"),
            ("Physics-based animations", "Spring and friction animations"),
        ],
        "architecture": [
            ("BLoC Pattern", "Business Logic Component pattern for state management"),
            ("MVVM", "Model-View-ViewModel architecture pattern"),
            ("Clean Architecture", "Domain-driven design with clear separation"),
            ("Repository Pattern", "Abstraction layer for data sources"),
            ("Provider Pattern", "Dependency injection and state management"),
            ("GetX Pattern", "Reactive state management with GetX"),
            ("MVC", "Model-View-Controller pattern in Flutter"),
            ("Redux", "Predictable state container pattern"),
            ("Riverpod Architecture", "Modern reactive caching framework"),
            ("Domain Driven Design", "DDD principles in Flutter"),
            ("Hexagonal Architecture", "Ports and adapters pattern"),
            ("Feature-based structure", "Organize code by features"),
        ],
        "testing": [
            ("Widget Testing", "Testing Flutter widgets in isolation"),
            ("Integration Testing", "End-to-end testing of Flutter apps"),
            ("Unit Testing", "Testing Dart code logic"),
            ("Golden Testing", "Visual regression testing"),
            ("Mockito", "Mocking framework for Dart"),
            ("flutter_test", "Flutter testing framework"),
            ("test", "Dart testing package"),
            ("integration_test", "Flutter integration testing"),
            ("mocktail", "Type-safe mocking library"),
            ("Test Coverage", "Measuring test completeness"),
            ("TDD", "Test-driven development"),
            ("BDD", "Behavior-driven development"),
        ],
        "performance": [
            ("Performance Profiling", "Analyzing app performance"),
            ("Widget Inspector", "Debugging widget trees"),
            ("Timeline View", "Performance timeline analysis"),
            ("Memory Profiling", "Analyzing memory usage"),
            ("Shader Compilation", "Reducing shader jank"),
            ("Build Optimization", "Optimizing build methods"),
            ("Lazy Loading", "Loading content on demand"),
            ("Image Caching", "Efficient image loading"),
            ("Code Splitting", "Reducing initial bundle size"),
            ("Tree Shaking", "Removing unused code"),
            ("Const Constructors", "Compile-time optimizations"),
            ("RepaintBoundary", "Isolate expensive paints"),
        ],
        "platform": [
            ("Platform Channels", "Communication with native code"),
            ("Method Channel", "Invoking platform-specific APIs"),
            ("Event Channel", "Streaming data from platform"),
            ("iOS Integration", "Flutter iOS-specific features"),
            ("Android Integration", "Flutter Android-specific features"),
            ("Web Support", "Flutter web-specific features"),
            ("Desktop Support", "Flutter desktop applications"),
            ("Embedding Flutter", "Adding Flutter to existing apps"),
            ("Platform Views", "Embedding native views"),
            ("FFI", "Foreign Function Interface"),
            ("Plugin Development", "Creating Flutter plugins"),
            ("Platform-specific UI", "Adaptive UI patterns"),
        ],
        "debugging": [
            ("Flutter Inspector", "Visual debugging tool"),
            ("Logging", "Debug logging in Flutter"),
            ("Breakpoints", "Using breakpoints in Flutter"),
            ("DevTools", "Flutter DevTools suite"),
            ("Error Handling", "Handling errors in Flutter"),
            ("Crash Reporting", "Capturing and reporting crashes"),
            ("Debug Mode", "Flutter debug mode features"),
            ("Assert Statements", "Debug-only checks"),
            ("Stack Traces", "Understanding error traces"),
            ("Network Debugging", "Inspecting network requests"),
            ("Layout Explorer", "Visualize layout constraints"),
            ("Performance Overlay", "On-device performance metrics"),
        ],
        "forms": [
            ("Form", "Container for form fields"),
            ("TextFormField", "Text input with validation"),
            ("FormField", "Base class for form fields"),
            ("Form Validation", "Validating user input"),
            ("Input Decoration", "Styling form fields"),
            ("Focus Management", "Managing input focus"),
            ("Keyboard Actions", "Custom keyboard actions"),
            ("Input Formatters", "Format input as typed"),
            ("Form State", "Managing form state"),
            ("Custom Form Fields", "Creating custom inputs"),
        ],
        "theming": [
            ("ThemeData", "Application theme configuration"),
            ("Material Theme", "Material Design theming"),
            ("Dark Mode", "Supporting dark theme"),
            ("Custom Themes", "Creating custom themes"),
            ("Theme Extensions", "Extending theme data"),
            ("Color Schemes", "Material 3 color system"),
            ("Typography", "Text theming"),
            ("Dynamic Theming", "Runtime theme changes"),
            ("Platform Theming", "Platform-specific themes"),
        ],
    }
    
    # Check if query matches any concept
    for concept, items in concepts.items():
        if concept in query_lower or any(word in concept for word in query_lower.split()):
            for item_name, item_desc in items:
                results.append({
                    "type": "concept",
                    "relevance": 0.8,
                    "title": item_name,
                    "description": item_desc,
                    "concept": concept
                })
    
    # Apply type filtering if specified
    if types:
        filtered_results = []
        for result in results:
            result_type = result.get("type", "")
            # Map result types to filter types
            if "flutter" in types and result_type == "flutter_class":
                filtered_results.append(result)
            elif "dart" in types and result_type == "dart_class":
                filtered_results.append(result)
            elif "package" in types and result_type == "pub_package":
                filtered_results.append(result)
            elif "concept" in types and result_type == "concept":
                filtered_results.append(result)
        results = filtered_results
    
    # Sort results by relevance
    results.sort(key=lambda x: x["relevance"], reverse=True)
    
    # Apply limit
    results = results[:limit]
    
    # Fetch actual documentation for top results if needed
    enriched_results = []
    for result in results:
        if result["type"] == "flutter_class" and "class_name" in result:
            # Only fetch full docs for top 3 Flutter classes
            if len(enriched_results) < 3:
                try:
                    doc = await _get_flutter_docs_impl(result["class_name"], result["library"])
                    if not doc.get("error"):
                        result["documentation_available"] = True
                        result["content_preview"] = doc.get("content", "")[:300] + "..."
                    else:
                        result["documentation_available"] = False
                        result["error_info"] = doc.get("error_type", "unknown")
                except Exception as e:
                    logger.warning("search_enrichment_error", error=str(e), class_name=result.get("class_name"))
                    result["documentation_available"] = False
                    result["error_info"] = "enrichment_failed"
        elif result["type"] == "pub_package" and "package_name" in result:
            # Add pub.dev URL
            result["url"] = f"https://pub.dev/packages/{result['package_name']}"
            result["documentation_available"] = True
        
        enriched_results.append(result)
    
    # Prepare final response
    response = {
        "query": query,
        "results": enriched_results,
        "total": len(enriched_results),
        "timestamp": datetime.utcnow().isoformat(),
        "suggestions": generate_search_suggestions(query_lower, enriched_results)
    }
    
    # Cache the search results for 1 hour
    cache_manager.set(cache_key, response, 3600)
    
    return response


def calculate_relevance(query: str, title: str, description: str) -> float:
    """Calculate relevance score based on fuzzy matching."""
    score = 0.0
    
    # Exact match in title
    if query == title:
        score += 1.0
    # Partial match in title
    elif query in title:
        score += 0.8
    # Word match in title
    elif any(word in title for word in query.split()):
        score += 0.6
    
    # Match in description
    if query in description:
        score += 0.4
    elif any(word in description for word in query.split() if len(word) > 3):
        score += 0.2
    
    # Fuzzy match using character overlap
    title_overlap = len(set(query) & set(title)) / len(set(query) | set(title)) if title else 0
    desc_overlap = len(set(query) & set(description)) / len(set(query) | set(description)) if description else 0
    score += (title_overlap * 0.3 + desc_overlap * 0.1)
    
    return min(score, 1.0)


def generate_search_suggestions(query: str, results: List[Dict]) -> List[str]:
    """Generate helpful search suggestions based on query and results."""
    suggestions = []
    
    if not results:
        suggestions.append(f"Try searching for specific widget names like 'Container' or 'Scaffold'")
        suggestions.append(f"Use package names from pub.dev like 'provider' or 'dio'")
        suggestions.append(f"Search for concepts like 'state management' or 'navigation'")
    elif len(results) < 3:
        suggestions.append(f"For more results, try broader terms or related concepts")
        if any(r["type"] == "flutter_class" for r in results):
            suggestions.append(f"You can also search for specific libraries like 'material.AppBar'")
    
    return suggestions


@mcp.tool()
async def flutter_docs(
    identifier: str,
    topic: Optional[str] = None,
    tokens: int = 10000
) -> Dict[str, Any]:
    """
    Unified tool to get Flutter/Dart documentation with smart identifier resolution.
    
    Automatically detects the type of identifier and fetches appropriate documentation.
    Supports Flutter classes, Dart classes, and pub.dev packages.
    
    Args:
        identifier: The identifier to look up. Examples:
                   - "Container" (Flutter widget)
                   - "material.AppBar" (library-qualified Flutter class)
                   - "dart:async.Future" (Dart API)
                   - "provider" (pub.dev package)
                   - "pub:dio" (explicit pub.dev package)
                   - "flutter:Container" (explicit Flutter class)
        topic: Optional topic filter. For classes: "constructors", "methods", 
               "properties", "examples". For packages: "getting-started", 
               "examples", "api", "installation"
        tokens: Maximum tokens for response (default: 10000, min: 1000)
    
    Returns:
        Dictionary with documentation content, type, and metadata
    """
    bind_contextvars(tool="flutter_docs", identifier=identifier, topic=topic)
    logger.info("resolving_identifier", identifier=identifier)
    
    # Validate tokens parameter
    if tokens < 1000:
        return {"error": "tokens parameter must be at least 1000"}
    
    # Parse identifier to determine type
    identifier_lower = identifier.lower()
    doc_type = None
    library = None
    class_name = None
    package_name = None
    
    # Check for explicit prefixes
    if identifier.startswith("pub:"):
        doc_type = "pub_package"
        package_name = identifier[4:]
    elif identifier.startswith("flutter:"):
        doc_type = "flutter_class"
        class_name = identifier[8:]
        library = "widgets"  # Default to widgets
    elif identifier.startswith("dart:"):
        doc_type = "dart_class"
        # Parse dart:library.Class format
        parts = identifier.split(".")
        if len(parts) == 2:
            library = parts[0]
            class_name = parts[1]
        else:
            class_name = identifier[5:]
            library = "dart:core"
    elif "." in identifier:
        # Library-qualified name (e.g., material.AppBar)
        parts = identifier.split(".", 1)
        library = parts[0]
        class_name = parts[1]
        
        if library.startswith("dart:"):
            doc_type = "dart_class"
        else:
            doc_type = "flutter_class"
    else:
        # Auto-detect type by trying different sources
        # First check if it's a known Flutter class
        flutter_libs = ["widgets", "material", "cupertino", "painting", "animation", 
                       "rendering", "services", "gestures", "foundation"]
        
        # Try to find in common Flutter widgets
        for lib in flutter_libs:
            test_url = f"https://api.flutter.dev/flutter/{lib}/{identifier}-class.html"
            if identifier.lower() in ["container", "scaffold", "appbar", "column", "row", 
                                    "text", "button", "listview", "gridview", "stack"]:
                doc_type = "flutter_class"
                class_name = identifier
                library = "widgets" if identifier.lower() in ["container", "column", "row", "text", "stack"] else "material"
                break
        
        if not doc_type:
            # Could be a package or unknown Flutter class
            # We'll try both and see what works
            doc_type = "auto"
            class_name = identifier
            package_name = identifier
    
    # Based on detected type, fetch documentation
    result = {
        "identifier": identifier,
        "type": doc_type,
        "topic": topic,
        "max_tokens": tokens
    }
    
    if doc_type == "flutter_class" or (doc_type == "auto" and class_name):
        # Try Flutter documentation first
        flutter_doc = await get_flutter_docs(class_name, library or "widgets", tokens=tokens)
        
        if "error" not in flutter_doc:
            # Successfully found Flutter documentation
            content = flutter_doc.get("content", "")
            
            # Apply topic filtering if requested
            if topic:
                content = filter_documentation_by_topic(content, topic, "flutter_class")
                # Recount tokens after filtering
                filtered_tokens = token_manager.count_tokens(content)
                # If filtering reduced content below token limit, no need for further truncation
                if tokens and filtered_tokens > tokens:
                    content = truncate_flutter_docs(content, class_name, tokens, strategy="balanced")
                    final_tokens = token_manager.count_tokens(content)
                else:
                    final_tokens = filtered_tokens
            else:
                # Use the token count from get_flutter_docs if no filtering
                final_tokens = flutter_doc.get("token_count", token_manager.count_tokens(content))
            
            result.update({
                "type": "flutter_class",
                "class": class_name,
                "library": flutter_doc.get("library"),
                "content": content,
                "source": flutter_doc.get("source"),
                "truncated": flutter_doc.get("truncated", False) or topic is not None,
                "token_count": final_tokens,
                "original_tokens": flutter_doc.get("original_tokens", final_tokens),
                "truncation_note": flutter_doc.get("truncation_note")
            })
            return result
        elif doc_type != "auto":
            # Explicit Flutter class not found
            return {
                "identifier": identifier,
                "type": "flutter_class",
                "error": flutter_doc.get("error"),
                "suggestion": flutter_doc.get("suggestion")
            }
    
    if doc_type == "dart_class":
        # Try Dart documentation
        dart_doc = await get_flutter_docs(class_name, library, tokens=tokens)
        
        if "error" not in dart_doc:
            content = dart_doc.get("content", "")
            
            # Apply topic filtering if requested
            if topic:
                content = filter_documentation_by_topic(content, topic, "dart_class")
                # Recount tokens after filtering
                filtered_tokens = token_manager.count_tokens(content)
                # If filtering reduced content below token limit, no need for further truncation
                if tokens and filtered_tokens > tokens:
                    content = truncate_flutter_docs(content, class_name, tokens, strategy="balanced")
                    final_tokens = token_manager.count_tokens(content)
                else:
                    final_tokens = filtered_tokens
            else:
                # Use the token count from get_flutter_docs if no filtering
                final_tokens = dart_doc.get("token_count", token_manager.count_tokens(content))
            
            result.update({
                "type": "dart_class",
                "class": class_name,
                "library": library,
                "content": content,
                "source": dart_doc.get("source"),
                "truncated": dart_doc.get("truncated", False) or topic is not None,
                "token_count": final_tokens,
                "original_tokens": dart_doc.get("original_tokens", final_tokens),
                "truncation_note": dart_doc.get("truncation_note")
            })
            return result
        else:
            return {
                "identifier": identifier,
                "type": "dart_class",
                "error": dart_doc.get("error"),
                "suggestion": "Check the class name and library. Example: dart:async.Future"
            }
    
    if doc_type == "pub_package" or doc_type == "auto":
        # Try pub.dev package
        package_doc = await _get_pub_package_info_impl(package_name)
        
        if "error" not in package_doc:
            # Successfully found package
            # Format content based on topic
            if topic:
                content = format_package_content_by_topic(package_doc, topic)
            else:
                content = format_package_content(package_doc)
            
            # Count original tokens
            original_tokens = token_manager.count_tokens(content)
            truncated = False
            truncation_note = None
            
            # Apply token truncation if needed
            if tokens and original_tokens > tokens:
                truncator = create_truncator(tokens)
                content = truncator.truncate(content)
                truncated = True
                truncation_note = f"Documentation truncated from {original_tokens} to approximately {tokens} tokens"
            
            # Count final tokens
            final_tokens = token_manager.count_tokens(content)
            
            result.update({
                "type": "pub_package",
                "package": package_name,
                "version": package_doc.get("version"),
                "content": content,
                "source": package_doc.get("source"),
                "metadata": {
                    "description": package_doc.get("description"),
                    "homepage": package_doc.get("homepage"),
                    "repository": package_doc.get("repository"),
                    "likes": package_doc.get("likes"),
                    "pub_points": package_doc.get("pub_points"),
                    "platforms": package_doc.get("platforms")
                },
                "truncated": truncated or topic is not None,
                "token_count": final_tokens,
                "original_tokens": original_tokens if truncated else final_tokens,
                "truncation_note": truncation_note
            })
            return result
        elif doc_type == "pub_package":
            # Explicit package not found
            return {
                "identifier": identifier,
                "type": "pub_package",
                "error": package_doc.get("error"),
                "suggestion": "Check the package name on pub.dev"
            }
    
    # If auto-detection failed to find anything
    if doc_type == "auto":
        # Try search as last resort
        search_results = await search_flutter_docs(identifier)
        if search_results.get("results"):
            top_result = search_results["results"][0]
            return {
                "identifier": identifier,
                "type": "search_suggestion",
                "error": f"Could not find exact match for '{identifier}'",
                "suggestion": f"Did you mean '{top_result['title']}'?",
                "search_results": search_results["results"][:3]
            }
        else:
            return {
                "identifier": identifier,
                "type": "not_found",
                "error": f"No documentation found for '{identifier}'",
                "suggestion": "Try using explicit prefixes like 'pub:', 'flutter:', or 'dart:'"
            }
    
    # Should not reach here
    return {
        "identifier": identifier,
        "type": "error",
        "error": "Failed to resolve identifier"
    }


def filter_documentation_by_topic(content: str, topic: str, doc_type: str) -> str:
    """Filter documentation content by topic"""
    topic_lower = topic.lower()
    
    if doc_type in ["flutter_class", "dart_class"]:
        # Class documentation topics
        lines = content.split('\n')
        filtered_lines = []
        current_section = None
        include_section = False
        
        for line in lines:
            # Detect section headers
            if line.startswith('## '):
                section_name = line[3:].lower()
                current_section = section_name
                
                # Determine if we should include this section
                if topic_lower == "constructors" and "constructor" in section_name:
                    include_section = True
                elif topic_lower == "methods" and "method" in section_name:
                    include_section = True
                elif topic_lower == "properties" and "propert" in section_name:
                    include_section = True
                elif topic_lower == "examples" and ("example" in section_name or "code" in section_name):
                    include_section = True
                else:
                    include_section = False
            
            # Always include the class name and description
            if line.startswith('# ') or (current_section == "description" and not line.startswith('## ')):
                filtered_lines.append(line)
            elif include_section:
                filtered_lines.append(line)
        
        return '\n'.join(filtered_lines)
    
    return content


def format_package_content(package_doc: Dict[str, Any]) -> str:
    """Format package documentation into readable content"""
    content = []
    
    # Header
    content.append(f"# {package_doc['name']} v{package_doc['version']}")
    content.append("")
    
    # Description
    content.append("## Description")
    content.append(package_doc.get('description', 'No description available'))
    content.append("")
    
    # Metadata
    content.append("## Package Information")
    content.append(f"- **Version**: {package_doc['version']}")
    content.append(f"- **Published**: {package_doc.get('updated', 'Unknown')}")
    content.append(f"- **Publisher**: {package_doc.get('publisher', 'Unknown')}")
    content.append(f"- **Platforms**: {', '.join(package_doc.get('platforms', []))}")
    content.append(f"- **Likes**: {package_doc.get('likes', 0)}")
    content.append(f"- **Pub Points**: {package_doc.get('pub_points', 0)}")
    content.append(f"- **Popularity**: {package_doc.get('popularity', 0)}")
    content.append("")
    
    # Links
    if package_doc.get('homepage') or package_doc.get('repository'):
        content.append("## Links")
        if package_doc.get('homepage'):
            content.append(f"- **Homepage**: {package_doc['homepage']}")
        if package_doc.get('repository'):
            content.append(f"- **Repository**: {package_doc['repository']}")
        if package_doc.get('documentation'):
            content.append(f"- **Documentation**: {package_doc['documentation']}")
        content.append("")
    
    # Dependencies
    if package_doc.get('dependencies'):
        content.append("## Dependencies")
        for dep in package_doc['dependencies']:
            content.append(f"- {dep}")
        content.append("")
    
    # Environment
    if package_doc.get('environment'):
        content.append("## Environment")
        for key, value in package_doc['environment'].items():
            content.append(f"- **{key}**: {value}")
        content.append("")
    
    # README
    if package_doc.get('readme'):
        content.append("## README")
        content.append(package_doc['readme'])
    
    return '\n'.join(content)


def format_package_content_by_topic(package_doc: Dict[str, Any], topic: str) -> str:
    """Format package documentation filtered by topic"""
    topic_lower = topic.lower()
    content = []
    
    # Always include header
    content.append(f"# {package_doc['name']} v{package_doc['version']}")
    content.append("")
    
    if topic_lower == "installation":
        content.append("## Installation")
        content.append("")
        content.append("Add this to your package's `pubspec.yaml` file:")
        content.append("")
        content.append("```yaml")
        content.append("dependencies:")
        content.append(f"  {package_doc['name']}: ^{package_doc['version']}")
        content.append("```")
        content.append("")
        content.append("Then run:")
        content.append("```bash")
        content.append("flutter pub get")
        content.append("```")
        
        # Include environment requirements
        if package_doc.get('environment'):
            content.append("")
            content.append("### Requirements")
            for key, value in package_doc['environment'].items():
                content.append(f"- **{key}**: {value}")
                
    elif topic_lower == "getting-started":
        content.append("## Getting Started")
        content.append("")
        content.append(package_doc.get('description', 'No description available'))
        content.append("")
        
        # Extract getting started section from README if available
        if package_doc.get('readme'):
            readme_lower = package_doc['readme'].lower()
            # Look for getting started section
            start_idx = readme_lower.find("getting started")
            if start_idx == -1:
                start_idx = readme_lower.find("quick start")
            if start_idx == -1:
                start_idx = readme_lower.find("usage")
            
            if start_idx != -1:
                # Extract section
                readme_section = package_doc['readme'][start_idx:]
                # Find next section header
                next_section = readme_section.find("\n## ")
                if next_section != -1:
                    readme_section = readme_section[:next_section]
                content.append(readme_section)
                
    elif topic_lower == "examples":
        content.append("## Examples")
        content.append("")
        
        # Extract examples from README
        if package_doc.get('readme'):
            readme = package_doc['readme']
            # Find code blocks
            code_blocks = re.findall(r'```[\w]*\n(.*?)\n```', readme, re.DOTALL)
            if code_blocks:
                for i, code in enumerate(code_blocks[:5]):  # Limit to 5 examples
                    content.append(f"### Example {i+1}")
                    content.append("```dart")
                    content.append(code)
                    content.append("```")
                    content.append("")
            else:
                content.append("No code examples found in documentation.")
                
    elif topic_lower == "api":
        content.append("## API Reference")
        content.append("")
        content.append(f"Full API documentation: https://pub.dev/documentation/{package_doc['name']}/latest/")
        content.append("")
        
        # Include basic package info
        content.append("### Package Information")
        content.append(f"- **Version**: {package_doc['version']}")
        content.append(f"- **Platforms**: {', '.join(package_doc.get('platforms', []))}")
        
        if package_doc.get('dependencies'):
            content.append("")
            content.append("### Dependencies")
            for dep in package_doc['dependencies']:
                content.append(f"- {dep}")
    
    else:
        # Default to full content for unknown topics
        return format_package_content(package_doc)
    
    return '\n'.join(content)


@mcp.tool()
async def process_flutter_mentions(text: str, tokens: int = 4000) -> Dict[str, Any]:
    """
    Parse text for @flutter_mcp mentions and return relevant documentation.
    
    NOTE: This tool is maintained for backward compatibility. For new integrations,
    consider using the unified tools directly:
    - flutter_docs: For Flutter/Dart classes and pub.dev packages
    - flutter_search: For searching Flutter/Dart documentation
    
    Supports patterns like:
    - @flutter_mcp provider (pub.dev package - latest version)
    - @flutter_mcp provider:^6.0.0 (specific version constraint)
    - @flutter_mcp riverpod:2.5.1 (exact version)
    - @flutter_mcp dio:>=5.0.0 <6.0.0 (version range)
    - @flutter_mcp bloc:latest (latest version keyword)
    - @flutter_mcp material.AppBar (Flutter class)
    - @flutter_mcp dart:async.Future (Dart API)
    - @flutter_mcp Container (widget)
    
    Args:
        text: Text containing @flutter_mcp mentions
        tokens: Maximum token limit for each mention's documentation (default: 4000, min: 500)
    
    Returns:
        Dictionary with parsed mentions and their documentation
    """
    bind_contextvars(tool="process_flutter_mentions", text_length=len(text))
    
    # Validate tokens parameter
    if tokens < 500:
        return {"error": "tokens parameter must be at least 500"}
    
    # Updated pattern to match @flutter_mcp mentions with version constraints
    # Now supports version constraints like :^6.0.0, :>=5.0.0 <6.0.0, etc.
    pattern = r'@flutter_mcp\s+([a-zA-Z0-9_.:]+(?:\s*[<>=^]+\s*[0-9.+\-\w]+(?:\s*[<>=]+\s*[0-9.+\-\w]+)?)?)'
    mentions = re.findall(pattern, text)
    
    if not mentions:
        return {
            "mentions_found": 0,
            "message": "No @flutter_mcp mentions found in text",
            "results": []
        }
    
    logger.info("mentions_found", count=len(mentions))
    results = []
    
    # Process each mention using the unified flutter_docs tool
    for mention in mentions:
        logger.info("processing_mention", mention=mention)
        
        try:
            # Parse version constraints if present
            if ':' in mention and not mention.startswith('dart:'):
                # Package with version constraint
                parts = mention.split(':', 1)
                identifier = parts[0]
                version_spec = parts[1]
                
                # For packages with version constraints, use get_pub_package_info
                if version_spec and version_spec != 'latest':
                    # Extract actual version if it's a simple version number
                    version = None
                    if re.match(r'^\d+\.\d+\.\d+$', version_spec.strip()):
                        version = version_spec.strip()
                    
                    # Get package with specific version
                    doc_result = await get_pub_package_info(identifier, version=version)
                    
                    if "error" not in doc_result:
                        results.append({
                            "mention": mention,
                            "type": "pub_package",
                            "documentation": doc_result
                        })
                        if version_spec and version_spec != version:
                            results[-1]["documentation"]["version_constraint"] = version_spec
                    else:
                        results.append({
                            "mention": mention,
                            "type": "package_version_error",
                            "error": doc_result["error"]
                        })
                else:
                    # Latest version requested
                    doc_result = await flutter_docs(identifier, max_tokens=tokens)
            else:
                # Use unified flutter_docs for all other cases
                doc_result = await flutter_docs(mention, max_tokens=tokens)
            
            # Process the result from flutter_docs
            if "error" not in doc_result:
                # Determine type based on result
                doc_type = doc_result.get("type", "unknown")
                
                if doc_type == "flutter_class":
                    results.append({
                        "mention": mention,
                        "type": "flutter_class",
                        "documentation": doc_result
                    })
                elif doc_type == "dart_class":
                    results.append({
                        "mention": mention,
                        "type": "dart_api",
                        "documentation": doc_result
                    })
                elif doc_type == "pub_package":
                    results.append({
                        "mention": mention,
                        "type": "pub_package",
                        "documentation": doc_result
                    })
                else:
                    # Fallback for auto-detected types
                    results.append({
                        "mention": mention,
                        "type": doc_result.get("type", "flutter_widget"),
                        "documentation": doc_result
                    })
            else:
                # Try search as fallback
                search_result = await flutter_search(mention, limit=1)
                if search_result.get("results"):
                    results.append({
                        "mention": mention,
                        "type": search_result["results"][0].get("type", "flutter_widget"),
                        "documentation": search_result["results"][0]
                    })
                else:
                    results.append({
                        "mention": mention,
                        "type": "not_found",
                        "error": f"No documentation found for '{mention}'"
                    })
                    
        except Exception as e:
            logger.error("mention_processing_error", mention=mention, error=str(e))
            results.append({
                "mention": mention,
                "type": "error",
                "error": f"Error processing mention: {str(e)}"
            })
    
    # Format results - keep the same format for backward compatibility
    formatted_results = []
    for result in results:
        if "error" in result:
            formatted_results.append({
                "mention": result["mention"],
                "type": result["type"],
                "error": result["error"]
            })
        else:
            doc = result["documentation"]
            if result["type"] == "pub_package":
                # Format package info
                formatted_result = {
                    "mention": result["mention"],
                    "type": "pub_package",
                    "name": doc.get("name", ""),
                    "version": doc.get("version", ""),
                    "description": doc.get("description", ""),
                    "documentation_url": doc.get("documentation", ""),
                    "dependencies": doc.get("dependencies", []),
                    "likes": doc.get("likes", 0),
                    "pub_points": doc.get("pub_points", 0)
                }
                
                # Add version constraint info if present
                if "version_constraint" in doc:
                    formatted_result["version_constraint"] = doc["version_constraint"]
                if "resolved_version" in doc:
                    formatted_result["resolved_version"] = doc["resolved_version"]
                    
                formatted_results.append(formatted_result)
            else:
                # Format Flutter/Dart documentation
                formatted_results.append({
                    "mention": result["mention"],
                    "type": result["type"],
                    "class": doc.get("class", doc.get("identifier", "")),
                    "library": doc.get("library", ""),
                    "content": doc.get("content", ""),
                    "source": doc.get("source", "live")
                })
    
    return {
        "mentions_found": len(mentions),
        "unique_mentions": len(set(mentions)),
        "results": formatted_results,
        "timestamp": datetime.utcnow().isoformat(),
        "note": "This tool is maintained for backward compatibility. Consider using flutter_docs or flutter_search directly."
    }


def clean_readme_markdown(readme_content: str) -> str:
    """Clean and format README markdown for AI consumption"""
    if not readme_content:
        return "No README available"
    
    # Remove HTML comments
    readme_content = re.sub(r'<!--.*?-->', '', readme_content, flags=re.DOTALL)
    
    # Remove excessive blank lines
    readme_content = re.sub(r'\n{3,}', '\n\n', readme_content)
    
    # Remove badges/shields (common in READMEs but not useful for AI)
    readme_content = re.sub(r'!\[.*?\]\(.*?shields\.io.*?\)', '', readme_content)
    readme_content = re.sub(r'!\[.*?\]\(.*?badge.*?\)', '', readme_content)
    
    # Clean up any remaining formatting issues
    readme_content = readme_content.strip()
    
    return readme_content


@mcp.tool()
async def get_pub_package_info(package_name: str, version: Optional[str] = None, tokens: int = 6000) -> Dict[str, Any]:
    """
    Get package information from pub.dev including README content.
    
    **DEPRECATED**: This tool is deprecated. Please use flutter_docs() instead
    with the "pub:" prefix (e.g., flutter_docs("pub:provider")).
    
    Args:
        package_name: Name of the pub.dev package (e.g., "provider", "bloc", "dio")
        version: Optional specific version to fetch (e.g., "6.0.5", "2.5.1")
        tokens: Maximum token limit for response (default: 6000, min: 500)
    
    Returns:
        Package information including version, description, metadata, and README
    """
    bind_contextvars(tool="get_pub_package_info", package=package_name, version=version)
    logger.warning("deprecated_tool_usage", tool="get_pub_package_info", replacement="flutter_docs")
    
    # Validate tokens parameter
    if tokens < 500:
        return {"error": "tokens parameter must be at least 500"}
    
    # Call new flutter_docs tool
    identifier = f"pub:{package_name}"
    if version:
        identifier += f":{version}"
    
    result = await flutter_docs(identifier, max_tokens=tokens)
    
    # Transform back to old format
    if result.get("error"):
        return {
            "error": result["error"]
        }
    else:
        metadata = result.get("metadata", {})
        return {
            "source": result.get("source", "live"),
            "name": result.get("package", package_name),
            "version": result.get("version", "latest"),
            "description": metadata.get("description", ""),
            "homepage": metadata.get("homepage", ""),
            "repository": metadata.get("repository", ""),
            "documentation": f"https://pub.dev/packages/{package_name}",
            "dependencies": [],  # Not included in new format
            "readme": result.get("content", ""),
            "pub_points": metadata.get("pub_points", 0),
            "likes": metadata.get("likes", 0),
            "fetched_at": datetime.utcnow().isoformat()
        }


async def _get_pub_package_info_impl(package_name: str, version: Optional[str] = None) -> Dict[str, Any]:
    """
    Internal implementation of get_pub_package_info functionality.
    """
    
    # Check cache first
    cache_key = get_cache_key("pub_package", package_name, version)
    
    # Check cache
    cached_data = cache_manager.get(cache_key)
    if cached_data:
        logger.info("cache_hit")
        return cached_data
    
    # Rate limit before fetching
    await rate_limiter.acquire()
    
    # Fetch from pub.dev API
    url = f"https://pub.dev/api/packages/{package_name}"
    logger.info("fetching_package", url=url)
    
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Fetch package info
            response = await client.get(
                url,
                headers={
                    "User-Agent": "Flutter-MCP-Docs/1.0 (github.com/flutter-mcp/flutter-mcp)"
                }
            )
            response.raise_for_status()
            
            data = response.json()
            
            # If specific version requested, find it in versions list
            if version:
                version_data = None
                for v in data.get("versions", []):
                    if v.get("version") == version:
                        version_data = v
                        break
                
                if not version_data:
                    return {
                        "error": f"Version '{version}' not found for package '{package_name}'",
                        "available_versions": [v.get("version") for v in data.get("versions", [])][:10]  # Show first 10
                    }
                
                pubspec = version_data.get("pubspec", {})
                actual_version = version_data.get("version", version)
                published_date = version_data.get("published", "")
            else:
                # Use latest version
                latest = data.get("latest", {})
                pubspec = latest.get("pubspec", {})
                actual_version = latest.get("version", "unknown")
                published_date = latest.get("published", "")
            
            result = {
                "source": "live",
                "name": package_name,
                "version": actual_version,
                "description": pubspec.get("description", "No description available"),
                "homepage": pubspec.get("homepage", ""),
                "repository": pubspec.get("repository", ""),
                "documentation": pubspec.get("documentation", f"https://pub.dev/packages/{package_name}"),
                "dependencies": list(pubspec.get("dependencies", {}).keys()),
                "dev_dependencies": list(pubspec.get("dev_dependencies", {}).keys()),
                "environment": pubspec.get("environment", {}),
                "platforms": data.get("platforms", []),
                "updated": published_date,
                "publisher": data.get("publisher", ""),
                "likes": data.get("likeCount", 0),
                "pub_points": data.get("pubPoints", 0),
                "popularity": data.get("popularityScore", 0)
            }
            
            # Fetch README content from package page
            # For specific versions, pub.dev uses /versions/{version} path
            if version:
                readme_url = f"https://pub.dev/packages/{package_name}/versions/{actual_version}"
            else:
                readme_url = f"https://pub.dev/packages/{package_name}"
            logger.info("fetching_readme", url=readme_url)
            
            try:
                # Rate limit before second request
                await rate_limiter.acquire()
                
                readme_response = await client.get(
                    readme_url,
                    headers={
                        "User-Agent": "Flutter-MCP-Docs/1.0 (github.com/flutter-mcp/flutter-mcp)"
                    }
                )
                readme_response.raise_for_status()
                
                # Parse page HTML to extract README
                soup = BeautifulSoup(readme_response.text, 'html.parser')
                
                # Find the README content - pub.dev uses a section with specific classes
                readme_div = soup.find('section', class_='detail-tab-readme-content')
                if not readme_div:
                    # Try finding any section with markdown-body class
                    readme_div = soup.find('section', class_='markdown-body')
                    if not readme_div:
                        # Try finding div with markdown-body
                        readme_div = soup.find('div', class_='markdown-body')
                
                if readme_div:
                    # Extract text content and preserve basic markdown structure
                    # Convert common HTML elements back to markdown
                    for br in readme_div.find_all('br'):
                        br.replace_with('\n')
                    
                    for p in readme_div.find_all('p'):
                        p.insert_after('\n\n')
                    
                    for h1 in readme_div.find_all('h1'):
                        h1.insert_before('# ')
                        h1.insert_after('\n\n')
                    
                    for h2 in readme_div.find_all('h2'):
                        h2.insert_before('## ')
                        h2.insert_after('\n\n')
                    
                    for h3 in readme_div.find_all('h3'):
                        h3.insert_before('### ')
                        h3.insert_after('\n\n')
                    
                    for code in readme_div.find_all('code'):
                        if code.parent.name != 'pre':
                            code.insert_before('`')
                            code.insert_after('`')
                    
                    for pre in readme_div.find_all('pre'):
                        code_block = pre.find('code')
                        if code_block:
                            lang_class = code_block.get('class', [])
                            lang = ''
                            for cls in lang_class if isinstance(lang_class, list) else [lang_class]:
                                if cls and cls.startswith('language-'):
                                    lang = cls.replace('language-', '')
                                    break
                            pre.insert_before(f'\n```{lang}\n')
                            pre.insert_after('\n```\n')
                    
                    readme_text = readme_div.get_text()
                    result["readme"] = clean_readme_markdown(readme_text)
                else:
                    result["readme"] = "README parsing failed - content structure not recognized"
                    
            except httpx.HTTPStatusError as e:
                logger.warning("readme_fetch_failed", status_code=e.response.status_code)
                result["readme"] = f"README not available (HTTP {e.response.status_code})"
            except Exception as e:
                logger.warning("readme_fetch_error", error=str(e))
                result["readme"] = f"Failed to fetch README: {str(e)}"
            
            # Cache for 12 hours
            cache_manager.set(cache_key, result, CACHE_DURATIONS["pub_package"])
            
            logger.info("package_fetched_success", has_readme="readme" in result)
            return result
            
    except httpx.HTTPStatusError as e:
        logger.error("http_error", status_code=e.response.status_code)
        return {
            "error": f"Package '{package_name}' not found on pub.dev",
            "status_code": e.response.status_code
        }
    except Exception as e:
        logger.error("fetch_error", error=str(e))
        return {
            "error": f"Failed to fetch package information: {str(e)}"
        }


@mcp.tool()
async def flutter_search(query: str, limit: int = 10, tokens: int = 5000) -> Dict[str, Any]:
    """
    Search across multiple Flutter/Dart documentation sources with unified results.
    
    Searches Flutter classes, Dart classes, pub packages, and concepts in parallel.
    Returns structured results with relevance scoring and documentation hints.
    
    Args:
        query: Search query (e.g., "state management", "Container", "http")
        limit: Maximum number of results to return (default: 10, max: 25)
        tokens: Maximum token limit for response (default: 5000, min: 500)
    
    Returns:
        Unified search results with type classification and relevance scores
    """
    bind_contextvars(tool="flutter_search", query=query, limit=limit)
    logger.info("unified_search_started")
    
    # Validate tokens parameter
    if tokens < 500:
        return {"error": "tokens parameter must be at least 500"}
    
    # Validate limit
    limit = min(max(limit, 1), 25)
    
    # Check cache for search results
    cache_key = get_cache_key("unified_search", f"{query}:{limit}")
    cached_data = cache_manager.get(cache_key)
    if cached_data:
        logger.info("unified_search_cache_hit")
        return cached_data
    
    # Prepare search tasks for parallel execution
    search_tasks = []
    results = []
    query_lower = query.lower()
    
    # Define search functions for parallel execution
    async def search_flutter_classes():
        """Search Flutter widget/class documentation"""
        flutter_results = []
        
        # Check if query is a direct Flutter class reference
        if url := resolve_flutter_url(query):
            # Extract class and library info from resolved URL
            library = "widgets"  # Default
            if "flutter/material" in url:
                library = "material"
            elif "flutter/cupertino" in url:
                library = "cupertino"
            elif "flutter/animation" in url:
                library = "animation"
            elif "flutter/painting" in url:
                library = "painting"
            elif "flutter/rendering" in url:
                library = "rendering"
            elif "flutter/services" in url:
                library = "services"
            elif "flutter/gestures" in url:
                library = "gestures"
            elif "flutter/foundation" in url:
                library = "foundation"
            
            class_match = re.search(r'/([^/]+)-class\.html$', url)
            if class_match:
                class_name = class_match.group(1)
                flutter_results.append({
                    "id": f"flutter:{library}:{class_name}",
                    "type": "flutter_class",
                    "relevance": 1.0,
                    "title": class_name,
                    "library": library,
                    "description": f"Flutter {library} class",
                    "doc_size": "large",
                    "url": url
                })
        
        # Search common Flutter classes
        flutter_classes = [
            # State management
            ("StatefulWidget", "widgets", "Base class for widgets that have mutable state", ["state", "stateful", "widget"]),
            ("StatelessWidget", "widgets", "Base class for widgets that don't require mutable state", ["state", "stateless", "widget"]),
            ("State", "widgets", "Logic and internal state for a StatefulWidget", ["state", "lifecycle"]),
            ("InheritedWidget", "widgets", "Base class for widgets that propagate information down the tree", ["inherited", "propagate", "state"]),
            ("ValueListenableBuilder", "widgets", "Rebuilds when ValueListenable changes", ["value", "listenable", "builder", "state"]),
            
            # Layout widgets
            ("Container", "widgets", "A convenience widget that combines common painting, positioning, and sizing", ["container", "box", "layout"]),
            ("Row", "widgets", "Displays children in a horizontal array", ["row", "horizontal", "layout"]),
            ("Column", "widgets", "Displays children in a vertical array", ["column", "vertical", "layout"]),
            ("Stack", "widgets", "Positions children relative to the box edges", ["stack", "overlay", "position"]),
            ("Scaffold", "material", "Basic material design visual layout structure", ["scaffold", "material", "layout", "structure"]),
            
            # Navigation
            ("Navigator", "widgets", "Manages a stack of Route objects", ["navigator", "navigation", "route"]),
            ("MaterialPageRoute", "material", "A modal route that replaces the entire screen", ["route", "navigation", "page"]),
            
            # Input widgets
            ("TextField", "material", "A material design text field", ["text", "input", "field", "form"]),
            ("GestureDetector", "widgets", "Detects gestures on widgets", ["gesture", "touch", "tap", "click"]),
            
            # Lists
            ("ListView", "widgets", "Scrollable list of widgets", ["list", "scroll", "view"]),
            ("GridView", "widgets", "Scrollable 2D array of widgets", ["grid", "scroll", "view"]),
            
            # Visual
            ("AppBar", "material", "A material design app bar", ["app", "bar", "header", "material"]),
            ("Card", "material", "A material design card", ["card", "material"]),
            
            # Async
            ("FutureBuilder", "widgets", "Builds based on interaction with a Future", ["future", "async", "builder"]),
            ("StreamBuilder", "widgets", "Builds based on interaction with a Stream", ["stream", "async", "builder"]),
        ]
        
        for class_name, library, description, keywords in flutter_classes:
            # Calculate relevance based on query match
            relevance = 0.0
            
            # Direct match
            if query_lower == class_name.lower():
                relevance = 1.0
            elif query_lower in class_name.lower():
                relevance = 0.8
            elif class_name.lower() in query_lower:
                relevance = 0.7
            
            # Keyword match
            if relevance < 0.3:
                for keyword in keywords:
                    if keyword in query_lower or query_lower in keyword:
                        relevance = max(relevance, 0.5)
                        break
            
            # Description match
            if relevance < 0.3 and query_lower in description.lower():
                relevance = 0.4
            
            if relevance > 0.3:
                flutter_results.append({
                    "id": f"flutter:{library}:{class_name}",
                    "type": "flutter_class",
                    "relevance": relevance,
                    "title": class_name,
                    "library": library,
                    "description": description,
                    "doc_size": "large"
                })
        
        return flutter_results
    
    async def search_dart_classes():
        """Search Dart core library documentation"""
        dart_results = []
        
        dart_classes = [
            ("List", "dart:core", "An indexable collection of objects with a length", ["list", "array", "collection"]),
            ("Map", "dart:core", "A collection of key/value pairs", ["map", "dictionary", "hash", "key", "value"]),
            ("Set", "dart:core", "A collection of objects with no duplicate elements", ["set", "unique", "collection"]),
            ("String", "dart:core", "A sequence of UTF-16 code units", ["string", "text"]),
            ("Future", "dart:async", "Represents a computation that completes with a value or error", ["future", "async", "promise"]),
            ("Stream", "dart:async", "A source of asynchronous data events", ["stream", "async", "event"]),
            ("Duration", "dart:core", "A span of time", ["duration", "time", "span"]),
            ("DateTime", "dart:core", "An instant in time", ["date", "time", "datetime"]),
            ("RegExp", "dart:core", "A regular expression pattern", ["regex", "regexp", "pattern"]),
            ("Iterable", "dart:core", "A collection of values that can be accessed sequentially", ["iterable", "collection", "sequence"]),
        ]
        
        for class_name, library, description, keywords in dart_classes:
            relevance = 0.0
            
            # Direct match
            if query_lower == class_name.lower():
                relevance = 1.0
            elif query_lower in class_name.lower():
                relevance = 0.8
            elif class_name.lower() in query_lower:
                relevance = 0.7
            
            # Keyword match
            if relevance < 0.3:
                for keyword in keywords:
                    if keyword in query_lower or query_lower in keyword:
                        relevance = max(relevance, 0.5)
                        break
            
            # Description match
            if relevance < 0.3 and query_lower in description.lower():
                relevance = 0.4
            
            if relevance > 0.3:
                dart_results.append({
                    "id": f"dart:{library.replace('dart:', '')}:{class_name}",
                    "type": "dart_class",
                    "relevance": relevance,
                    "title": class_name,
                    "library": library,
                    "description": description,
                    "doc_size": "medium"
                })
        
        return dart_results
    
    async def search_pub_packages():
        """Search pub.dev packages"""
        package_results = []
        
        # Define popular packages with categories
        packages = [
            # State Management
            ("provider", "State management library that makes it easy to connect business logic to widgets", ["state", "management", "provider"], "state_management"),
            ("riverpod", "A reactive caching and data-binding framework", ["state", "management", "riverpod", "reactive"], "state_management"),
            ("bloc", "State management library implementing the BLoC design pattern", ["state", "management", "bloc", "pattern"], "state_management"),
            ("get", "Open source state management, navigation and utilities", ["state", "management", "get", "navigation"], "state_management"),
            
            # Networking
            ("dio", "Powerful HTTP client for Dart with interceptors and FormData", ["http", "network", "dio", "api"], "networking"),
            ("http", "A composable, multi-platform, Future-based API for HTTP requests", ["http", "network", "request"], "networking"),
            ("retrofit", "Type-safe HTTP client generator", ["http", "network", "retrofit", "generator"], "networking"),
            
            # Storage
            ("shared_preferences", "Flutter plugin for reading and writing simple key-value pairs", ["storage", "preferences", "settings"], "storage"),
            ("sqflite", "SQLite plugin for Flutter", ["database", "sqlite", "sql", "storage"], "storage"),
            ("hive", "Lightweight and blazing fast key-value database", ["database", "hive", "nosql", "storage"], "storage"),
            
            # Firebase
            ("firebase_core", "Flutter plugin to use Firebase Core API", ["firebase", "core", "backend"], "firebase"),
            ("firebase_auth", "Flutter plugin for Firebase Auth", ["firebase", "auth", "authentication"], "firebase"),
            ("cloud_firestore", "Flutter plugin for Cloud Firestore", ["firebase", "firestore", "database"], "firebase"),
            
            # UI/UX
            ("flutter_svg", "SVG rendering and widget library for Flutter", ["svg", "image", "vector", "ui"], "ui"),
            ("cached_network_image", "Flutter library to load and cache network images", ["image", "cache", "network", "ui"], "ui"),
            ("animations", "Beautiful pre-built animations for Flutter", ["animation", "transition", "ui"], "ui"),
            
            # Navigation
            ("go_router", "A declarative routing package for Flutter", ["navigation", "router", "routing"], "navigation"),
            ("auto_route", "Code generation for type-safe route navigation", ["navigation", "router", "generation"], "navigation"),
            
            # Platform
            ("url_launcher", "Flutter plugin for launching URLs", ["url", "launcher", "platform"], "platform"),
            ("path_provider", "Flutter plugin for getting commonly used locations on filesystem", ["path", "file", "platform"], "platform"),
            ("image_picker", "Flutter plugin for selecting images", ["image", "picker", "camera", "gallery"], "platform"),
        ]
        
        for package_name, description, keywords, category in packages:
            relevance = 0.0
            
            # Direct match
            if query_lower == package_name:
                relevance = 1.0
            elif query_lower in package_name:
                relevance = 0.8
            elif package_name in query_lower:
                relevance = 0.7
            
            # Keyword match
            if relevance < 0.3:
                for keyword in keywords:
                    if keyword in query_lower or query_lower in keyword:
                        relevance = max(relevance, 0.6)
                        break
            
            # Category match
            if relevance < 0.3 and category in query_lower:
                relevance = 0.5
            
            # Description match
            if relevance < 0.3 and query_lower in description.lower():
                relevance = 0.4
            
            if relevance > 0.3:
                package_results.append({
                    "id": f"pub:{package_name}",
                    "type": "pub_package",
                    "relevance": relevance,
                    "title": package_name,
                    "category": category,
                    "description": description,
                    "doc_size": "variable",
                    "url": f"https://pub.dev/packages/{package_name}"
                })
        
        return package_results
    
    async def search_concepts():
        """Search programming concepts and patterns"""
        concept_results = []
        
        concepts = {
            "state_management": {
                "title": "State Management in Flutter",
                "description": "Techniques for managing application state",
                "keywords": ["state", "management", "provider", "bloc", "riverpod"],
                "related": ["setState", "InheritedWidget", "provider", "bloc", "riverpod", "get"]
            },
            "navigation": {
                "title": "Navigation & Routing",
                "description": "Moving between screens and managing navigation stack",
                "keywords": ["navigation", "routing", "navigator", "route", "screen"],
                "related": ["Navigator", "MaterialPageRoute", "go_router", "deep linking"]
            },
            "async_programming": {
                "title": "Asynchronous Programming",
                "description": "Working with Futures, Streams, and async operations",
                "keywords": ["async", "future", "stream", "await", "asynchronous"],
                "related": ["Future", "Stream", "FutureBuilder", "StreamBuilder", "async/await"]
            },
            "http_networking": {
                "title": "HTTP & Networking",
                "description": "Making HTTP requests and handling network operations",
                "keywords": ["http", "network", "api", "rest", "request"],
                "related": ["http", "dio", "retrofit", "REST API", "JSON"]
            },
            "database_storage": {
                "title": "Database & Storage",
                "description": "Persisting data locally using various storage solutions",
                "keywords": ["database", "storage", "sqlite", "persistence", "cache"],
                "related": ["sqflite", "hive", "shared_preferences", "drift", "objectbox"]
            },
            "animation": {
                "title": "Animations in Flutter",
                "description": "Creating smooth animations and transitions",
                "keywords": ["animation", "transition", "animate", "motion"],
                "related": ["AnimationController", "AnimatedBuilder", "Hero", "Curves"]
            },
            "testing": {
                "title": "Testing Flutter Apps",
                "description": "Unit, widget, and integration testing strategies",
                "keywords": ["test", "testing", "unit", "widget", "integration"],
                "related": ["flutter_test", "mockito", "integration_test", "golden tests"]
            },
            "architecture": {
                "title": "App Architecture Patterns",
                "description": "Organizing code with architectural patterns",
                "keywords": ["architecture", "pattern", "mvvm", "mvc", "clean"],
                "related": ["BLoC Pattern", "MVVM", "Clean Architecture", "Repository Pattern"]
            },
            "performance": {
                "title": "Performance Optimization",
                "description": "Improving app performance and reducing jank",
                "keywords": ["performance", "optimization", "speed", "jank", "profile"],
                "related": ["Performance Profiling", "Widget Inspector", "const constructors"]
            },
            "platform_integration": {
                "title": "Platform Integration",
                "description": "Integrating with native platform features",
                "keywords": ["platform", "native", "channel", "integration", "plugin"],
                "related": ["Platform Channels", "Method Channel", "Plugin Development"]
            }
        }
        
        for concept_id, concept_data in concepts.items():
            relevance = 0.0
            
            # Check keywords
            for keyword in concept_data["keywords"]:
                if keyword in query_lower or query_lower in keyword:
                    relevance = max(relevance, 0.7)
                    
            # Check title
            if query_lower in concept_data["title"].lower():
                relevance = max(relevance, 0.8)
                
            # Check description
            if relevance < 0.3 and query_lower in concept_data["description"].lower():
                relevance = 0.5
            
            if relevance > 0.3:
                concept_results.append({
                    "id": f"concept:{concept_id}",
                    "type": "concept",
                    "relevance": relevance,
                    "title": concept_data["title"],
                    "description": concept_data["description"],
                    "related_items": concept_data["related"],
                    "doc_size": "summary"
                })
        
        return concept_results
    
    # Execute all searches in parallel
    flutter_task = asyncio.create_task(search_flutter_classes())
    dart_task = asyncio.create_task(search_dart_classes())
    pub_task = asyncio.create_task(search_pub_packages())
    concept_task = asyncio.create_task(search_concepts())
    
    # Wait for all searches to complete
    flutter_results, dart_results, pub_results, concept_results = await asyncio.gather(
        flutter_task, dart_task, pub_task, concept_task
    )
    
    # Combine all results
    all_results = flutter_results + dart_results + pub_results + concept_results
    
    # Sort by relevance and limit
    all_results.sort(key=lambda x: x["relevance"], reverse=True)
    results = all_results[:limit]
    
    # Add search metadata
    response = {
        "query": query,
        "total_results": len(all_results),
        "returned_results": len(results),
        "results": results,
        "result_types": {
            "flutter_classes": sum(1 for r in results if r["type"] == "flutter_class"),
            "dart_classes": sum(1 for r in results if r["type"] == "dart_class"),
            "pub_packages": sum(1 for r in results if r["type"] == "pub_package"),
            "concepts": sum(1 for r in results if r["type"] == "concept")
        },
        "timestamp": datetime.utcnow().isoformat()
    }
    
    # Add search suggestions if results are limited
    if len(results) < 5:
        suggestions = []
        if not any(r["type"] == "flutter_class" for r in results):
            suggestions.append("Try searching for specific widget names like 'Container' or 'Scaffold'")
        if not any(r["type"] == "pub_package" for r in results):
            suggestions.append("Search for package names like 'provider' or 'dio'")
        if not any(r["type"] == "concept" for r in results):
            suggestions.append("Try broader concepts like 'state management' or 'navigation'")
        
        response["suggestions"] = suggestions
    
    # Cache the results for 1 hour
    cache_manager.set(cache_key, response, 3600)
    
    logger.info("unified_search_completed", 
                total_results=len(all_results),
                returned_results=len(results))
    
    return response


@mcp.tool()
async def flutter_status() -> Dict[str, Any]:
    """
    Check the health status of all Flutter documentation services.
    
    Returns:
        Health status including individual service checks and cache statistics
    """
    checks = {}
    overall_status = "ok"
    timestamp = datetime.utcnow().isoformat()
    
    # Check Flutter docs scraper
    flutter_start = time.time()
    try:
        # Test with Container widget - a stable, core widget unlikely to be removed
        result = await get_flutter_docs("Container", "widgets")
        flutter_duration = int((time.time() - flutter_start) * 1000)
        
        if "error" in result:
            checks["flutter_docs"] = {
                "status": "failed",
                "target": "Container widget",
                "duration_ms": flutter_duration,
                "error": result["error"]
            }
            overall_status = "degraded"
        else:
            checks["flutter_docs"] = {
                "status": "ok",
                "target": "Container widget",
                "duration_ms": flutter_duration,
                "cached": result.get("source") == "cache"
            }
    except Exception as e:
        flutter_duration = int((time.time() - flutter_start) * 1000)
        checks["flutter_docs"] = {
            "status": "failed",
            "target": "Container widget",
            "duration_ms": flutter_duration,
            "error": str(e)
        }
        overall_status = "failed"
    
    # Check pub.dev scraper
    pub_start = time.time()
    try:
        # Test with provider package - extremely popular, unlikely to be removed
        result = await get_pub_package_info("provider")
        pub_duration = int((time.time() - pub_start) * 1000)
        
        if result is None:
            checks["pub_dev"] = {
                "status": "timeout",
                "target": "provider package",
                "duration_ms": pub_duration,
                "error": "Health check timed out after 10 seconds"
            }
            overall_status = "degraded" if overall_status == "ok" else overall_status
        elif result.get("error"):
            checks["pub_dev"] = {
                "status": "failed",
                "target": "provider package",
                "duration_ms": pub_duration,
                "error": result.get("message", "Unknown error"),
                "error_type": result.get("error_type", "unknown")
            }
            overall_status = "degraded" if overall_status == "ok" else overall_status
        else:
            # Additional validation - check if we got expected fields
            has_version = "version" in result and result["version"] != "unknown"
            has_readme = "readme" in result and len(result.get("readme", "")) > 100
            
            if not has_version:
                checks["pub_dev"] = {
                    "status": "degraded",
                    "target": "provider package",
                    "duration_ms": pub_duration,
                    "error": "Could not parse version information",
                    "cached": result.get("source") == "cache"
                }
                overall_status = "degraded" if overall_status == "ok" else overall_status
            elif not has_readme:
                checks["pub_dev"] = {
                    "status": "degraded",
                    "target": "provider package",
                    "duration_ms": pub_duration,
                    "error": "Could not parse README content",
                    "cached": result.get("source") == "cache"
                }
                overall_status = "degraded" if overall_status == "ok" else overall_status
            else:
                checks["pub_dev"] = {
                    "status": "ok",
                    "target": "provider package",
                    "duration_ms": pub_duration,
                    "version": result["version"],
                    "cached": result.get("source") == "cache"
                }
    except Exception as e:
        pub_duration = int((time.time() - pub_start) * 1000)
        checks["pub_dev"] = {
            "status": "failed",
            "target": "provider package",
            "duration_ms": pub_duration,
            "error": str(e)
        }
        overall_status = "failed" if overall_status == "failed" else "degraded"
    
    # Check cache status
    try:
        cache_stats = cache_manager.get_stats()
        checks["cache"] = {
            "status": "ok",
            "message": "SQLite cache operational",
            "stats": cache_stats
        }
    except Exception as e:
        checks["cache"] = {
            "status": "degraded",
            "message": "Cache error",
            "error": str(e)
        }
        overall_status = "degraded"
    
    return {
        "status": overall_status,
        "timestamp": timestamp,
        "checks": checks,
        "message": get_health_message(overall_status)
    }


@mcp.tool()
async def health_check() -> Dict[str, Any]:
    """
    Check the health status of all scrapers and services.
    
    **DEPRECATED**: This tool is deprecated. Please use flutter_status() instead.
    
    Returns:
        Health status including individual scraper checks and overall status
    """
    logger.warning("deprecated_tool_usage", tool="health_check", replacement="flutter_status")
    
    # Simply call the new flutter_status function
    return await flutter_status()


def get_health_message(status: str) -> str:
    """Get a human-readable message for the health status"""
    messages = {
        "ok": "All systems operational",
        "degraded": "Service degraded - some features may be slow or unavailable",
        "failed": "Service failed - critical components are not working"
    }
    return messages.get(status, "Unknown status")


def main():
    """Main entry point for the Flutter MCP server"""
    import os
    
    # When running from CLI, the header is already printed
    # Only log when not running from CLI (e.g., direct execution)
    if not hasattr(sys, '_flutter_mcp_cli'):
        logger.info("flutter_mcp_starting", version="0.1.0")
    
    # Initialize cache and show stats
    try:
        cache_stats = cache_manager.get_stats()
        logger.info("cache_ready", stats=cache_stats)
    except Exception as e:
        logger.warning("cache_initialization_warning", error=str(e))
    
    # Get transport configuration from environment
    transport = os.environ.get('MCP_TRANSPORT', 'stdio')
    host = os.environ.get('MCP_HOST', '127.0.0.1')
    port = int(os.environ.get('MCP_PORT', '8000'))
    
    # Run the MCP server with appropriate transport
    # Note: host/port are configured in FastMCP constructor (at module import time via env vars)
    if transport == 'stdio':
        mcp.run()
    elif transport == 'sse':
        logger.info("starting_sse_transport", host=host, port=port)
        mcp.run(transport='sse')
    elif transport == 'http':
        logger.info("starting_http_transport", host=host, port=port)
        mcp.run(transport='streamable-http')


if __name__ == "__main__":
    main()