"""Logging utilities for beautiful terminal output"""

import humanize
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

console = Console(stderr=True)

def format_cache_stats(logger, method_name, event_dict):
    """
    A structlog processor to format 'cache_ready' event stats into a rich Table.
    """
    if event_dict.get("event") == "cache_ready" and "stats" in event_dict:
        stats = event_dict.pop("stats")
        
        table = Table(box=box.ROUNDED, show_header=False, title="Cache Stats", title_style="cyan bold")
        table.add_column("Stat", style="cyan")
        table.add_column("Value", style="magenta")

        table.add_row("Total Entries", str(stats.get('total_entries', 'N/A')))
        table.add_row("Active Entries", str(stats.get('active_entries', 'N/A')))
        table.add_row("Expired Entries", str(stats.get('expired_entries', 'N/A')))
        
        db_size = stats.get('database_size_bytes')
        if db_size is not None:
            table.add_row("Database Size", humanize.naturalsize(db_size))
            
        # Truncate path for display
        path = str(stats.get('database_path', 'N/A'))
        if len(path) > 40:
            path = f"...{path[-37:]}"
        table.add_row("Path", path)

        # Render the table to a string and modify the event message
        with console.capture() as capture:
            console.print(table)
        
        # Modify the event message itself to include the table
        event_dict["event"] = f"Cache ready\n{capture.get()}"
    return event_dict


def print_server_header():
    """Print a cool ASCII art header for the server startup"""
    ascii_art = """╔═══════════════════════════════════════════════════════════════════════════╗
║   _____ _       _   _            __  __  ____ ____                        ║
║  |  ___| |_   _| |_| |_ ___ _ __|  \\/  |/ ___|  _ \\                       ║
║  | |_  | | | | | __| __/ _ \\ '__| |\\/| | |   | |_) |                      ║
║  |  _| | | |_| | |_| ||  __/ |  | |  | | |___|  __/                       ║
║  |_|   |_|\\__,_|\\__|\\__\\___|_|  |_|  |_|\\____|_|                          ║
║                                                                           ║
║              🎯 Real-time Flutter/Dart Documentation Server               ║
╚═══════════════════════════════════════════════════════════════════════════╝"""
    
    title = Text("Flutter MCP Server", style="bold white")
    version = Text("v0.1.0", style="bold cyan")
    subtitle = Text("Connect your AI assistant to the Flutter universe", style="italic dim")
    
    header_text = Text.assemble(
        (ascii_art, "blue"),
        ("\n", ""),
        ("                               ", ""),
        version,
        ("\n                    ", ""),
        subtitle
    )

    panel = Panel(
        header_text,
        border_style="blue",
        expand=False,
        padding=(0, 2)
    )
    console.print(panel)