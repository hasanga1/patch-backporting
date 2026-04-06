#!/usr/bin/env python3
"""
Setup script for realistic Java backporting example with code refactoring.
Creates 3 versions: v1.0 (vulnerable), v2.0 (refactored + fixed), and backport scenario.
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
    repo_dir = "dataset/java-realistic"
    patch_dir = "patch_dataset/java-realistic/JAVA-REALISTIC-CVE-2024-88888"
    
    print("Setting up realistic Java backporting example...")
    print("This creates 3 versions to show real-world differences\n")
    
    # Clean up if repo exists
    if os.path.exists(os.path.join(repo_dir, ".git")):
        print("Cleaning existing repository...")
        run_cmd(f"rm -rf {repo_dir}/.git")
    
    # Initialize git repository
    print("Initializing git repository...")
    run_cmd(f"cd {repo_dir} && git init", check=False)
    run_cmd(f"cd {repo_dir} && git config user.email 'test@example.com'")
    run_cmd(f"cd {repo_dir} && git config user.name 'Test User'")
    
    # ===== VERSION 1.0: Original vulnerable code (old release) =====
    print("\n[V1.0] Creating vulnerable version...")
    
    # Create directory structure
    os.makedirs(os.path.join(repo_dir, "src/main/java/com/example/app"), exist_ok=True)
    os.makedirs(os.path.join(repo_dir, "src/main/java/com/example/security"), exist_ok=True)
    
    # Create v1.0 DatabaseUtils (old naming, all in one class)
    db_utils_v1 = os.path.join(repo_dir, "src/main/java/com/example/app/DatabaseUtils.java")
    with open(db_utils_v1, "w") as f:
        f.write('''package com.example.app;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

/**
 * DatabaseUtils - Main database utility class (v1.0)
 * Contains all database operations in one file
 */
public class DatabaseUtils {
    
    /**
     * Fetch user data by ID directly from database
     * @param connection Database connection  
     * @param userId User ID from user input
     * @return User data or null
     */
    public static String getUserData(Connection connection, String userId) {
        try {
            // VULNERABLE: Direct string concatenation
            String query = "SELECT data FROM users WHERE id = " + userId;
            Statement stmt = connection.createStatement();
            ResultSet rs = stmt.executeQuery(query);
            
            if (rs.next()) {
                return rs.getString("data");
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return null;
    }
    
    /**
     * Authenticate user with credentials
     * @param connection Database connection
     * @param username Username from login form
     * @param password Password from login form
     * @return true if auth succeeds
     */
    public static boolean login(Connection connection, String username, String password) {
        try {
            // VULNERABLE: Direct string concatenation with quotes
            String query = "SELECT * FROM users WHERE username = '" + username + 
                          "' AND password = '" + password + "'";
            Statement stmt = connection.createStatement();
            ResultSet rs = stmt.executeQuery(query);
            
            return rs.next();
        } catch (Exception e) {
            e.printStackTrace();
        }
        return false;
    }
}
''')
    
    run_cmd(f"cd {repo_dir} && git add .")
    run_cmd(f"cd {repo_dir} && git commit -m 'v1.0: Initial vulnerable version'")
    v1_hash = run_cmd(f"cd {repo_dir} && git rev-parse HEAD")
    print(f"V1.0 commit: {v1_hash}")
    
    # ===== VERSION 1.5: Intermediate version (refactored but still vulnerable) =====
    print("\n[V1.5] Creating intermediate version (refactored, still vulnerable)...")
    
    # Create separate SecurityUtils class (v1.5 refactoring)
    sec_utils_v15 = os.path.join(repo_dir, "src/main/java/com/example/security/SecurityUtils.java")
    with open(sec_utils_v15, "w") as f:
        f.write('''package com.example.security;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

/**
 * SecurityUtils - Extracted security functions (v1.5)
 * Refactored from DatabaseUtils into separate package
 */
public class SecurityUtils {
    
    /**
     * Authenticate user with credentials
     * @param connection Database connection
     * @param user Username from login form
     * @param pass Password from login form
     * @return true if authentication succeeds
     */
    public static boolean authenticate(Connection connection, String user, String pass) {
        try {
            // VULNERABLE: Direct string concatenation with quotes
            String sql = "SELECT * FROM users WHERE username = '" + user + 
                        "' AND password = '" + pass + "'";
            Statement stmt = connection.createStatement();
            ResultSet rs = stmt.executeQuery(sql);
            
            return rs.next();
        } catch (Exception e) {
            e.printStackTrace();
        }
        return false;
    }
    
    /**
     * Fetch user record by ID
     * @param connection Database connection
     * @param id User ID from request
     * @return User record or null
     */
    public static String fetchUserById(Connection connection, String id) {
        try {
            // VULNERABLE: Direct string concatenation
            String sql = "SELECT data FROM users WHERE id = " + id;
            Statement stmt = connection.createStatement();
            ResultSet rs = stmt.executeQuery(sql);
            
            if (rs.next()) {
                return rs.getString("data");
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return null;
    }
}
''')
    
    # Keep old DatabaseUtils but mark as deprecated
    with open(db_utils_v1, "w") as f:
        f.write('''package com.example.app;

import com.example.security.SecurityUtils;
import java.sql.Connection;

/**
 * DatabaseUtils - Deprecated, use SecurityUtils instead (v1.5)
 */
@Deprecated
public class DatabaseUtils {
    
    public static String getUserData(Connection connection, String userId) {
        return SecurityUtils.fetchUserById(connection, userId);
    }
    
    public static boolean login(Connection connection, String username, String password) {
        return SecurityUtils.authenticate(connection, username, password);
    }
}
''')
    
    run_cmd(f"cd {repo_dir} && git add .")
    run_cmd(f"cd {repo_dir} && git commit -m 'v1.5: Refactor into SecurityUtils package'")
    v15_hash = run_cmd(f"cd {repo_dir} && git rev-parse HEAD")
    print(f"V1.5 commit: {v15_hash}")
    
    # ===== VERSION 2.0: Latest mainline with fix + refactoring =====
    print("\n[V2.0] Creating mainline version (refactored + fixed)...")
    
    with open(sec_utils_v15, "w") as f:
        f.write('''package com.example.security;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;

/**
 * SecurityUtils - Secure database operations (v2.0)
 * Fixed SQL injection vulnerabilities with PreparedStatements
 */
public class SecurityUtils {
    
    /**
     * Authenticate user with credentials securely
     * @param connection Database connection
     * @param user Username from login form
     * @param pass Password from login form
     * @return true if authentication succeeds
     */
    public static boolean authenticate(Connection connection, String user, String pass) {
        try {
            // FIXED: Uses PreparedStatement to prevent SQL injection
            String sql = "SELECT * FROM users WHERE username = ? AND password = ?";
            PreparedStatement pstmt = connection.prepareStatement(sql);
            pstmt.setString(1, user);
            pstmt.setString(2, pass);
            ResultSet rs = pstmt.executeQuery();
            
            return rs.next();
        } catch (Exception e) {
            e.printStackTrace();
        }
        return false;
    }
    
    /**
     * Fetch user record by ID securely
     * @param connection Database connection
     * @param id User ID from request
     * @return User record or null
     */
    public static String fetchUserById(Connection connection, String id) {
        try {
            // FIXED: Uses PreparedStatement to prevent SQL injection
            String sql = "SELECT data FROM users WHERE id = ?";
            PreparedStatement pstmt = connection.prepareStatement(sql);
            pstmt.setString(1, id);
            ResultSet rs = pstmt.executeQuery();
            
            if (rs.next()) {
                return rs.getString("data");
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return null;
    }
}
''')
    
    run_cmd(f"cd {repo_dir} && git add .")
    run_cmd(f"cd {repo_dir} && git commit -m 'v2.0: Fix SQL injection with PreparedStatements'")
    v2_hash = run_cmd(f"cd {repo_dir} && git rev-parse HEAD")
    print(f"V2.0 commit: {v2_hash}")
    
    # Generate patch from v1.5 to v2.0 (mainline patch with fix)
    print("\n[Mainline Patch] Generating v1.5 → v2.0 patch...")
    mainline_patch = run_cmd(f"cd {repo_dir} && git format-patch -1 --stdout {v15_hash}..{v2_hash}")
    
    patch_file = os.path.join(patch_dir, "fix.patch")
    with open(patch_file, "w") as f:
        f.write(mainline_patch)
    print(f"Mainline patch saved to: {patch_file}")
    
    # Generate what a backport attempt looks like: v1.0 → v2.0 (shows full refactoring + fix)
    print("\n[Backport Context] Generating v1.0 → v2.0 full diff...")
    full_diff = run_cmd(f"cd {repo_dir} && git diff {v1_hash}..{v2_hash}")
    
    with open(os.path.join(patch_dir, "full-diff-v1.0-to-v2.0.patch"), "w") as f:
        f.write(full_diff)
    print(f"Full diff saved (shows refactoring changes too)")
    
    # Print summary
    print("\n" + "="*70)
    print("Realistic Backporting Example Setup Complete!")
    print("="*70)
    print(f"\nVersion History:")
    print(f"  V1.0 (Vulnerable):    {v1_hash}")
    print(f"  V1.5 (Refactored):    {v15_hash}")
    print(f"  V2.0 (Fixed):         {v2_hash}")
    
    print(f"\nBackporting Scenario:")
    print(f"  Source (Mainline):    V2.0 - Fixed code with PreparedStatements")
    print(f"  Target (Old Release): V1.5 - Refactored but still vulnerable")
    print(f"  Challenge: Code structure is different!")
    
    print(f"\nUpdate src/java-realistic.yml with:")
    print(f"  new_patch: {v2_hash}          (mainline fix)")
    print(f"  new_patch_parent: {v15_hash}  (mainline parent)")
    print(f"  target_release: {v15_hash}    (backport target - v1.5)")
    
    print(f"\nKey Differences Between Patches:")
    print(f"  • Mainline patch (v1.5→v2.0): ONLY the SQL injection fix")
    print(f"  • Backport scenario (to v1.5):  Still has PreparedStatement changes")
    print(f"  • Full diff (v1.0→v2.0):       Includes refactoring + fix")
    
    print(f"\nThen run:")
    print(f"  python src/backporting.py --config src/java-realistic.yml --debug")

if __name__ == "__main__":
    main()
