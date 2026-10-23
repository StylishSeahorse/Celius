import { desktop } from "../../lib/desktop";

// Links in replies never navigate the app itself away; the desktop app opens them in the
// system browser.
export function ExternalLink({ node: _node, ...props }: React.ComponentProps<"a"> & { node?: unknown }) {
  return (
    <a
      {...props}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => {
        if (desktop && props.href) {
          event.preventDefault();
          desktop.openExternal(props.href);
        }
      }}
    />
  );
}
