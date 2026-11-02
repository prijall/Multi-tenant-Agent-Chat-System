import type { ChatCard, SlotOption, SlotsCard, StatusCard } from "@/lib/api";

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill ${status}`}>{status}</span>;
}

function StatusDetails({ card }: { card: StatusCard }) {
  return (
    <section className="status-card" aria-label={card.title}>
      <header>
        <h3>{card.title}</h3>
        {card.status && <StatusPill status={card.status} />}
      </header>
      {card.rows.map((row) => (
        <div className="status-row" key={`${row.label}-${row.value}`}>
          <div>
            <strong>{row.label}</strong>
            {row.status && <span>{row.value}</span>}
          </div>
          {row.status ? <StatusPill status={row.status} /> : <span>{row.value}</span>}
        </div>
      ))}
      {card.total && <div className="status-row status-total"><span>Total</span><strong>{card.total}</strong></div>}
      {!!card.timeline?.length && (
        <ol className="status-timeline">
          {card.timeline.map((step) => <li key={`${step.status}-${step.at}`}><strong>{step.status}</strong><span>{step.at}</span></li>)}
        </ol>
      )}
    </section>
  );
}

function SlotPicker({ card, disabled, onPick }: {
  card: SlotsCard;
  disabled: boolean;
  onPick: (card: SlotsCard, slot: SlotOption) => void;
}) {
  return (
    <section className="slots-card" aria-label={card.title}>
      <h3>{card.title}</h3>
      <div className="slot-options">
        {card.slots.map((slot) => (
          <button disabled={disabled} key={`${slot.practitioner_id}-${slot.starts_at}`} onClick={() => onPick(card, slot)}>
            <strong>{slot.label}</strong>
            <span>{slot.practitioner}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

export function ChatCards({ cards, disabled, onPick }: {
  cards: ChatCard[];
  disabled: boolean;
  onPick: (card: SlotsCard, slot: SlotOption) => void;
}) {
  return cards.map((card) => (card.type === "slots"
    ? <SlotPicker card={card} disabled={disabled} key={`slots-${card.title}`} onPick={onPick} />
    : <StatusDetails card={card} key={`status-${card.title}`} />));
}
