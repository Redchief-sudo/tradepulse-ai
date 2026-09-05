import { parseOccSymbol } from '../format'

/** DISPLAY-ONLY decode of an option position's OCC symbol -- see the
 * parse-only contract documented on `parseOccSymbol`. Never invents a
 * contract field; on a non-matching symbol it renders the raw symbol
 * string, exactly as the backend returned it. */
export function OccContractLabel({ symbol }: { symbol: string }) {
  const contract = parseOccSymbol(symbol)
  if (!contract) return <span>{symbol}</span>
  return (
    <span title={symbol}>
      {contract.root} {contract.expiry} {contract.right === 'C' ? 'Call' : 'Put'} ${contract.strike.toFixed(2)}
    </span>
  )
}
