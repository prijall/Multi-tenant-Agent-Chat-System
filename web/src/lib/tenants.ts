export type TenantTheme = {
  paper: string;
  surface: string;
  surfaceAlt: string;
  ink: string;
  line: string;
  accent: string;
  onAccent: string;
};

export type Tenant = {
  slug: string;
  name: string;
  greeting: string;
  description: string;
  kind: "store" | "studio";
  agentName: string;
  timezone: string;
  placeholder: string;
  suggestions: string[];
  theme: TenantTheme;
};

export const tenants: Tenant[] = [
  {
    slug: "himali-tea",
    name: "Himali Tea House",
    greeting: "Namaste! I can help you find a tea, place an order or track a delivery.",
    description: "Tea, teaware and orders",
    kind: "store",
    agentName: "Tea house assistant",
    timezone: "Asia/Kathmandu",
    placeholder: "Ask about teas, teaware or your orders",
    suggestions: [
      "What green teas do you have under Rs 800?",
      "How long does delivery take?",
      "Where is my latest order?",
    ],
    theme: {
      paper: "#f1efe3",
      surface: "#fdfcf5",
      surfaceAlt: "#e4e7d8",
      ink: "#17201b",
      line: "#d2d5c2",
      accent: "#244d3c",
      onAccent: "#ffffff",
    },
  },
  {
    slug: "lotus-wellness",
    name: "Lotus Wellness Studio",
    greeting: "Namaste! I can help you choose a service, book a session or change an appointment.",
    description: "Massage, physiotherapy and yoga bookings",
    kind: "studio",
    agentName: "Studio assistant",
    timezone: "Asia/Kathmandu",
    placeholder: "Ask about services or your appointments",
    suggestions: [
      "What massages do you offer?",
      "What is your cancellation policy?",
      "What are my upcoming bookings?",
    ],
    theme: {
      paper: "#f6eef3",
      surface: "#fffbfd",
      surfaceAlt: "#eddfe8",
      ink: "#2a1d27",
      line: "#e0cfda",
      accent: "#704c68",
      onAccent: "#ffffff",
    },
  },
];

export function findTenant(slug: string): Tenant | undefined {
  return tenants.find((tenant) => tenant.slug === slug);
}
