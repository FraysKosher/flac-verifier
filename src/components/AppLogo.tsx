interface AppLogoProps {
  size?: number;
  color?: string;
}

export function AppLogo({ size = 64, color = "currentColor" }: AppLogoProps) {
  // 5 bars aligned to y=74 baseline, left-to-right x: 10,23,36,49,62
  const bars = [
    { x: 10, h: 28 },
    { x: 23, h: 44 },
    { x: 36, h: 60 },
    { x: 49, h: 36 },
    { x: 62, h: 52 },
  ];
  const baseline = 74;

  return (
    <svg
      viewBox="0 0 80 80"
      width={size}
      height={size}
      fill="none"
      aria-label="FLAC Verifier logo"
    >
      {bars.map(({ x, h }, i) => (
        <rect
          key={i}
          x={x}
          y={baseline - h}
          width={8}
          height={h}
          rx={3}
          ry={3}
          fill={color}
        />
      ))}
      {/* Teal checkmark overlaid bottom-right */}
      <polyline
        points="57,68 62,74 76,59"
        stroke="#4f98a3"
        strokeWidth={3}
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}
