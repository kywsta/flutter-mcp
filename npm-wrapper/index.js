/**
 * Flutter MCP Server Node.js wrapper
 * Main entry point for programmatic usage
 */

import { execa } from 'execa';
import which from 'which';

class FlutterMCPServer {
  constructor(options = {}) {
    this.pythonCmd = null;
    this.options = {
      stdio: options.stdio || false,
      debug: options.debug || false,
      ...options
    };
  }

  async findPython() {
    if (this.pythonCmd) return this.pythonCmd;
    
    const pythonCommands = ['python3', 'python'];
    
    for (const cmd of pythonCommands) {
      try {
        await which(cmd);
        const { stdout } = await execa(cmd, ['--version']);
        const match = stdout.match(/Python (\d+)\.(\d+)/);
        if (match) {
          const major = parseInt(match[1]);
          const minor = parseInt(match[2]);
          if (major >= 3 && minor >= 8) {
            this.pythonCmd = cmd;
            return cmd;
          }
        }
      } catch (e) {
        // Continue to next command
      }
    }
    throw new Error('Python 3.8+ is required but not found');
  }

  async ensureInstalled() {
    const pythonCmd = await this.findPython();
    
    try {
      await execa(pythonCmd, ['-m', 'flutter_mcp', '--version']);
      return true;
    } catch (e) {
      // Try to install
      await execa(pythonCmd, ['-m', 'pip', 'install', 'flutter-mcp-server']);
      return true;
    }
  }

  async start() {
    await this.ensureInstalled();
    const pythonCmd = await this.findPython();
    
    const args = ['-m', 'flutter_mcp'];
    if (this.options.stdio) {
      args.push('--stdio');
    }
    
    const subprocess = execa(pythonCmd, args, {
      stdio: this.options.stdio ? 'inherit' : 'pipe'
    });
    
    return subprocess;
  }

  async stop() {
    // Implement stop functionality if needed
  }
}

export default FlutterMCPServer;