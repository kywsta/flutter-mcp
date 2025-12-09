#!/usr/bin/env node
/**
 * Flutter MCP Server wrapper for Node.js
 * This script ensures Python is available and runs the Flutter MCP server
 */

import { execa } from 'execa';
import which from 'which';
import ora from 'ora';
import path from 'node:path';
import fs from 'node:fs';

async function findPython() {
  const pythonCommands = ['python3', 'python'];
  
  for (const cmd of pythonCommands) {
    try {
      await which(cmd);
      // Verify it's Python 3.8+
      const { stdout } = await execa(cmd, ['--version']);
      const match = stdout.match(/Python (\d+)\.(\d+)/);
      if (match) {
        const major = parseInt(match[1]);
        const minor = parseInt(match[2]);
        if (major >= 3 && minor >= 8) {
          return cmd;
        }
      }
    } catch (e) {
      // Continue to next command
    }
  }
  return null;
}

async function installFlutterMCP(pythonCmd, forceInstall = false) {
  const spinner = ora('Checking Flutter MCP Server installation...').start();
  
  try {
    // Check if already installed
    if (!forceInstall) {
      try {
        await execa(pythonCmd, ['-m', 'flutter_mcp', '--version']);
        spinner.succeed('Flutter MCP Server is ready');
        return true;
      } catch (e) {
        // Not installed, continue with installation
      }
    }
    
    // Install using pip
    spinner.text = 'Installing Flutter MCP Server from PyPI...';
    
    // For npx usage, install to user directory to avoid permission issues
    const isNpx = process.env.npm_execpath && process.env.npm_execpath.includes('npx');
    const pipArgs = ['-m', 'pip', 'install', '--user', 'flutter-mcp'];
    
    if (!isNpx) {
      // For global install, try without --user first
      pipArgs.splice(pipArgs.indexOf('--user'), 1);
    }
    
    await execa(pythonCmd, pipArgs, {
      stdio: 'inherit'
    });
    
    spinner.succeed('Flutter MCP Server installed successfully');
    return true;
  } catch (error) {
    spinner.fail('Failed to install Flutter MCP Server');
    console.error(error.message);
    return false;
  }
}

async function runFlutterMCP(pythonCmd, args) {
  try {
    // Run the server
    await execa(pythonCmd, ['-m', 'flutter_mcp', ...args], {
      stdio: 'inherit'
    });
  } catch (error) {
    if (error.exitCode !== 0) {
      console.error('Flutter MCP Server exited with error');
      process.exit(error.exitCode);
    }
  }
}

async function main() {
  const args = process.argv.slice(2);
  
  // Check for help flag
  if (args.includes('--help') || args.includes('-h')) {
    console.log(`
Flutter MCP Server - Real-time Flutter/Dart documentation for AI assistants

Usage: 
  npx flutter-mcp [options]              # One-time usage
  npm install -g flutter-mcp             # Global installation
  flutter-mcp [options]                  # After global install

Options:
  --help, -h        Show this help message
  --version, -v     Show version information
  --install         Install/update the Python package
  --stdio           Run in stdio mode (default for MCP clients)
  --http            Run in HTTP mode
  --sse             Run in Server-Sent Events mode
  --port <port>     Port for HTTP/SSE mode (default: 3000)
  
Examples:
  # Quick start with npx (no installation)
  npx flutter-mcp
  
  # Install globally then use
  npm install -g flutter-mcp
  flutter-mcp
  
  # Use with Claude Desktop (stdio mode)
  npx flutter-mcp --stdio

Claude Desktop Configuration:
  {
    "mcpServers": {
      "flutter-docs": {
        "command": "npx",
        "args": ["flutter-mcp", "--stdio"]
      }
    }
  }

For more information, visit: https://github.com/flutter-mcp/flutter-mcp
`);
    process.exit(0);
  }
  
  // Find Python
  const spinner = ora('Checking Python installation...').start();
  const pythonCmd = await findPython();
  
  if (!pythonCmd) {
    spinner.fail('Python 3.8+ is required but not found');
    console.error(`
Please install Python 3.8 or later:
- macOS: brew install python3
- Ubuntu/Debian: sudo apt install python3 python3-pip
- Windows: https://www.python.org/downloads/
`);
    process.exit(1);
  }
  
  spinner.succeed(`Found Python: ${pythonCmd}`);
  
  // Install Flutter MCP if needed
  const forceInstall = args.includes('--install');
  const installed = await installFlutterMCP(pythonCmd, forceInstall);
  if (!installed) {
    process.exit(1);
  }
  
  // If --install flag was provided, exit here
  if (args.includes('--install') && !args.includes('--stdio')) {
    console.log('\nFlutter MCP Server is ready to use!');
    process.exit(0);
  }
  
  // Default to stdio mode if no transport specified
  if (!args.includes('--http') && !args.includes('--sse') && !args.includes('--stdio')) {
    args.push('--stdio');
  }
  
  // Run the server
  if (!args.includes('--stdio')) {
    console.log('\nStarting Flutter MCP Server...\n');
  }
  await runFlutterMCP(pythonCmd, args);
}

// Handle errors
process.on('unhandledRejection', (error) => {
  console.error('Unhandled error:', error);
  process.exit(1);
});

// Run main function
main().catch((error) => {
  console.error('Fatal error:', error);
  process.exit(1);
});