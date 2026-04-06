#!/usr/bin/env python3
"""
Setup script for Java example repository.
This script initializes the git repository with two commits: vulnerable and fixed versions.
"""

import os
import subprocess
import sys

def run_cmd(cmd, cwd=None, check=True):
    """Run a shell command and return the output."""
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"Error running: {cmd}")
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
        sys.exit(1)
    return result.stdout.strip()

def main():
    repo_dir = "dataset/java-example"
    patch_dir = "patch_dataset/java-example/JAVA-EXAMPLE-CVE-2024-99999"
    
    print("Setting up Java example repository...")
    
    # Clean up if repo exists
    if os.path.exists(os.path.join(repo_dir, ".git")):
        print("Cleaning existing repository...")
        run_cmd(f"rm -rf {repo_dir}/.git")
    
    # Initialize git repository
    print("Initializing git repository...")
    run_cmd(f"cd {repo_dir} && git init", check=False)
    run_cmd(f"cd {repo_dir} && git config user.email 'test@example.com'")
    run_cmd(f"cd {repo_dir} && git config user.name 'Test User'")
    
    # Create initial commit with vulnerable code
    print("Creating initial commit with vulnerable code...")
    run_cmd(f"cd {repo_dir} && git add .")
    run_cmd(f"cd {repo_dir} && git commit -m 'Initial vulnerable version'")
    parent_hash = run_cmd(f"cd {repo_dir} && git rev-parse HEAD")
    print(f"Parent commit: {parent_hash}")
    
    # Update SecurityUtils.java to fixed version
    print("Applying fix to SecurityUtils.java...")
    security_utils_path = os.path.join(repo_dir, "src/main/java/com/example/app/SecurityUtils.java")
    with open(security_utils_path, "w") as f:
        f.write('''package com.example.app;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;

/**
 * Utility class for database operations.
 * VULNERABLE: Contains SQL injection vulnerability
 */
public class SecurityUtils {
    
    /**
     * Fetch user data by ID from database.
     * WARNING: This method is vulnerable to SQL injection!
     * 
     * @param connection Database connection
     * @param userId User ID from user input
     * @return User data or null if not found
     */
    public static String getUserData(Connection connection, String userId) {
        try {
            // FIXED: Use prepared statement to prevent SQL injection
            String query = "SELECT data FROM users WHERE id = ?";
            PreparedStatement stmt = connection.prepareStatement(query);
            stmt.setString(1, userId);
            ResultSet rs = stmt.executeQuery();
            
            if (rs.next()) {
                return rs.getString("data");
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return null;
    }
    
    /**
     * Authenticate user with username and password.
     * WARNING: This method is vulnerable to SQL injection!
     * 
     * @param connection Database connection
     * @param username Username from user input
     * @param password Password from user input
     * @return true if authentication succeeds, false otherwise
     */
    public static boolean authenticateUser(Connection connection, String username, String password) {
        try {
            // FIXED: Use prepared statement to prevent SQL injection
            String query = "SELECT * FROM users WHERE username = ? AND password = ?";
            PreparedStatement stmt = connection.prepareStatement(query);
            stmt.setString(1, username);
            stmt.setString(2, password);
            ResultSet rs = stmt.executeQuery();
            
            return rs.next();
        } catch (Exception e) {
            e.printStackTrace();
        }
        return false;
    }
}
''')
    
    # Create commit with fix
    print("Creating commit with fix...")
    run_cmd(f"cd {repo_dir} && git add src/main/java/com/example/app/SecurityUtils.java")
    run_cmd(f"cd {repo_dir} && git commit -m 'Fix SQL injection vulnerability in SecurityUtils'")
    fix_hash = run_cmd(f"cd {repo_dir} && git rev-parse HEAD")
    print(f"Fix commit: {fix_hash}")
    
    # Generate patch file
    print("Generating patch file...")
    patch_output = run_cmd(f"cd {repo_dir} && git format-patch -1 --stdout {parent_hash}..{fix_hash}")
    
    patch_file = os.path.join(patch_dir, "fix.patch")
    with open(patch_file, "w") as f:
        f.write(patch_output)
    print(f"Patch saved to: {patch_file}")
    
    # Print summary
    print("\n" + "="*50)
    print("Setup Complete!")
    print("="*50)
    print(f"\nParent commit (vulnerable): {parent_hash}")
    print(f"Fix commit: {fix_hash}")
    print(f"Target release: {parent_hash}")
    print(f"\nUpdate src/java-example.yml with:")
    print(f"  new_patch: {fix_hash}")
    print(f"  new_patch_parent: {parent_hash}")
    print(f"  target_release: {parent_hash}")
    print(f"\nThen run:")
    print(f"  python src/backporting.py --config src/java-example.yml --debug")

if __name__ == "__main__":
    main()
