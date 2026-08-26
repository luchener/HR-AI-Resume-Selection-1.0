'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react';
import { useRouter, usePathname } from 'next/navigation';

const TOKEN_KEY = 'resume-screening-token';
const USER_KEY = 'resume-screening-user';

export interface AuthUser {
  user_id: string;
  username: string;
}

export function getStoredToken(): string {
  if (typeof window === 'undefined') return '';
  return window.localStorage.getItem(TOKEN_KEY) || '';
}

export function getStoredUser(): AuthUser | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as AuthUser) : null;
  } catch {
    return null;
  }
}

export function clearStoredAuth(): void {
  if (typeof window === 'undefined') return;
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
}

interface AuthContextValue {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isHydrated: boolean;
  login: (token: string, user: AuthUser) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isHydrated, setIsHydrated] = useState(false);

  useEffect(() => {
    // 首次挂载：从 localStorage 恢复登录态
    const token = getStoredToken();
    const storedUser = getStoredUser();
    setUser(token && storedUser ? storedUser : null);
    setIsHydrated(true);
  }, []);

  const isOnLoginPage = pathname === '/login';
  // 公开页面（未登录也可访问）：登录页 + 忘记密码重置页
  const isPublicPage = isOnLoginPage || pathname === '/reset-password';

  useEffect(() => {
    if (!isHydrated) return;
    const token = getStoredToken();
    if (!token && !isPublicPage) {
      // 未登录访问受保护页面 → 跳登录
      router.replace('/login');
    }
  }, [isHydrated, isPublicPage, router]);

  const login = useCallback((token: string, authUser: AuthUser) => {
    window.localStorage.setItem(TOKEN_KEY, token);
    window.localStorage.setItem(USER_KEY, JSON.stringify(authUser));
    setUser(authUser);
  }, []);

  const logout = useCallback(() => {
    clearStoredAuth();
    // 清除会话内的分析结果，避免用户切换后看到上一个用户的报告
    try {
      window.sessionStorage.removeItem('resume-screening-result');
    } catch {
      // ignore
    }
    setUser(null);
    router.replace('/login');
  }, [router]);

  const value: AuthContextValue = {
    user,
    isAuthenticated: Boolean(user),
    isHydrated,
    login,
    logout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used within AuthProvider');
  return context;
}
