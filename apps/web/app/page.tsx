import Link from "next/link";

export default function HomePage() {
  return (
    <main>
      <h1>ClipOS</h1>
      <p>Operator control surface.</p>
      <nav>
        <Link href="/agents">Agent Registry</Link>
        <Link href="/models">Model Registry</Link>
      </nav>
    </main>
  );
}
