import React from 'react';
import { Outlet, Link, useLocation } from 'react-router-dom';
import { LayoutDashboard, Bot, Eye } from 'lucide-react';
import { cn } from '@/lib/utils';

const navItems = [
  { path: '/', label: 'Dashboard', icon: LayoutDashboard },
  { path: '/ai-trader', label: 'AI Trader', icon: Bot },
  { path: '/watchlist', label: 'Watchlist', icon: Eye },
];

export default function Layout() {
  const location = useLocation();

  return (
    <div className="min-h-screen bg-background flex flex-col md:flex-row">
      <aside className="hidden md:flex flex-col w-64 border-r border-sidebar-border bg-sidebar fixed inset-y-0 left-0 z-40">
        <div className="flex items-center gap-2.5 px-6 h-16 border-b border-sidebar-border">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-primary to-accent flex items-center justify-center glow-primary">
            <Bot className="w-5 h-5 text-white" />
          </div>
          <span className="font-heading font-bold text-lg tracking-tight">
            AlphaTrade<span className="text-primary">AI</span>
          </span>
        </div>
        <nav className="flex-1 p-4 space-y-1">
          {navItems.map((item) => (
            <Link
              key={item.path}
              to={item.path}
              className={cn(
                'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-200',
                location.pathname === item.path
                  ? 'bg-primary/10 text-primary'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted/50'
              )}
            >
              <item.icon className="w-4 h-4" />
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="p-4 border-t border-sidebar-border">
          <div className="rounded-xl bg-muted/30 p-4">
            <p className="text-xs font-semibold text-foreground">AI-Powered Trading</p>
            <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
              Real-time market intelligence at your fingertips.
            </p>
          </div>
        </div>
      </aside>

      <main className="flex-1 md:ml-64">
        <Outlet />
      </main>

      <nav className="md:hidden fixed bottom-0 inset-x-0 z-40 bg-sidebar border-t border-sidebar-border backdrop-blur-lg">
        <div className="flex">
          {navItems.map((item) => (
            <Link
              key={item.path}
              to={item.path}
              className={cn(
                'flex-1 flex flex-col items-center gap-1 py-3 text-xs font-medium transition-colors',
                location.pathname === item.path ? 'text-primary' : 'text-muted-foreground'
              )}
            >
              <item.icon className="w-5 h-5" />
              {item.label}
            </Link>
          ))}
        </div>
      </nav>
    </div>
  );
}