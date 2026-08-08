import React, { useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { BookOpen, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import TradingSessionCard from '@/components/TradingSessionCard';
import TradingSessionDetail from '@/components/TradingSessionDetail';

export default function TradingJournal() {
  const [sessions, setSessions] = useState([]);
  const [selectedSession, setSelectedSession] = useState(null);
  const [reportData, setReportData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState(null);

  const fetchSessions = useCallback(async () => {
    try {
      setLoading(true);
      const data = await base44.entities.TradingSession.list('-session_date', 30);
      setSessions(data);
      if (data.length > 0 && !selectedSession) {
        setSelectedSession(data[0]);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [selectedSession]);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  const generateReport = async (date) => {
    try {
      setGenerating(true);
      setError(null);
      const result = await base44.functions.invoke('generateDailyTradingReport', {
        date: date || new Date().toISOString().slice(0, 10),
        final: false,
      });
      const data = result?.data || result;
      if (!data || data.ok === false || data.error) {
        throw new Error(data?.error || 'Failed to generate report');
      }
      setReportData(data);
      // Refresh session list
      await fetchSessions();
      // Select the generated session
      if (data.session) {
        setSelectedSession(data.session);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setGenerating(false);
    }
  };

  const loadSessionDetail = async (session) => {
    setSelectedSession(session);
    try {
      const result = await base44.functions.invoke('generateDailyTradingReport', {
        date: session.session_date,
      });
      const data = result?.data || result;
      if (data && data.ok) {
        setReportData(data);
      }
    } catch (e) {
      // Non-fatal — the summary card still shows
    }
  };

  return (
    <div className="p-4 md:p-8 max-w-7xl mx-auto pb-20 md:pb-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-primary to-accent flex items-center justify-center glow-primary">
            <BookOpen className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="font-heading font-bold text-xl">Trading Journal</h1>
            <p className="text-xs text-muted-foreground">Daily trading session reports & audit logs</p>
          </div>
        </div>
        <Button
          onClick={() => generateReport()}
          disabled={generating}
          className="gap-2"
        >
          {generating ? <RefreshCw className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          {generating ? 'Generating...' : "Generate Today's Snapshot"}
        </Button>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-destructive/10 text-destructive text-sm">
          {error}
        </div>
      )}

      <div className="flex flex-col lg:flex-row gap-4">
        {/* Session List */}
        <div className="lg:w-80 shrink-0">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2 px-1">
            Recent Sessions
          </h2>
          {loading ? (
            <div className="space-y-2">
              {[1, 2, 3].map((i) => (
                <div key={i} className="h-28 rounded-xl bg-muted/30 animate-pulse" />
              ))}
            </div>
          ) : sessions.length === 0 ? (
            <div className="p-6 rounded-xl border border-border bg-card text-center">
              <p className="text-sm text-muted-foreground">No sessions yet.</p>
              <p className="text-xs text-muted-foreground mt-1">
                Click "Generate Today's Snapshot" to create the first entry.
              </p>
            </div>
          ) : (
            <div className="space-y-2 max-h-[calc(100vh-200px)] overflow-y-auto scrollbar-thin pr-1">
              {sessions.map((s) => (
                <TradingSessionCard
                  key={s.id}
                  session={s}
                  isSelected={selectedSession?.id === s.id}
                  onClick={() => loadSessionDetail(s)}
                />
              ))}
            </div>
          )}
        </div>

        {/* Session Detail */}
        <div className="flex-1 min-w-0">
          {selectedSession ? (
            <TradingSessionDetail
              session={reportData?.session || selectedSession}
              trades={reportData?.trades || []}
              scanRuns={reportData?.scan_runs || []}
              auditEvents={reportData?.audit_events || []}
            />
          ) : (
            <div className="p-8 rounded-xl border border-border bg-card text-center">
              <BookOpen className="w-12 h-12 text-muted-foreground mx-auto mb-3 opacity-50" />
              <p className="text-sm text-muted-foreground">
                Select a session to view its detailed trading journal.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
