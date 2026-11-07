import { statusTone } from "../lib/format";

interface StatusBadgeProps {
  status: string;
  title?: string;
}

/** Coloured pill for an investigation, stage, verdict or proposal status. */
export function StatusBadge({ status, title }: StatusBadgeProps) {
  return (
    <span className={`badge badge--${statusTone(status)}`} title={title ?? status}>
      {status}
    </span>
  );
}
