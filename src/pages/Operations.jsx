import React from 'react';
import { motion } from 'framer-motion';
import IntentLifecycle from '@/components/operations/IntentLifecycle';
import ReconciliationLog from '@/components/operations/ReconciliationLog';
import DataFreshness from '@/components/operations/DataFreshness';

export default function Operations() {
  return (
    <div className="p-6 space-y-6 pb-20 md:pb-6">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="text-2xl font-bold font-heading">Operations</h1>
        <p className="text-sm text-muted-foreground mt-1">Execution pipeline health, broker reconciliation, and data freshness.</p>
      </motion.div>
      <IntentLifecycle />
      <ReconciliationLog />
      <DataFreshness />
    </div>
  );
}