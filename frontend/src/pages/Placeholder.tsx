export function PagePlaceholder({
  title,
  arrivesIn,
}: {
  title: string
  arrivesIn: string
}) {
  return (
    <section>
      <h1>{title}</h1>
      <p>Arrives in {arrivesIn}</p>
    </section>
  )
}
