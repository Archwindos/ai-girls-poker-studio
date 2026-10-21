"""Import one project-local candidate without selecting it or changing deck config."""
import argparse
import json

from library import Library, MAX_IMAGE_BYTES
from runtime_paths import project_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Source image inside the project')
    parser.add_argument('--id', required=True)
    parser.add_argument('--role')
    parser.add_argument('--kind', choices=['face', 'back', 'box'], default='face')
    parser.add_argument('--face', choices=['front', 'back', 'left', 'right', 'top', 'bottom'])
    parser.add_argument('--has-bleed', action='store_true')
    parser.add_argument('--back-safety', help='Optional project-local back safety JSON')
    parser.add_argument('--name')
    parser.add_argument('--layout', help='Optional project-local layout JSON')
    args = parser.parse_args()
    source = project_path(args.source)
    if source.stat().st_size > MAX_IMAGE_BYTES:
        parser.error('Source exceeds 25MiB')
    payload = {'id': args.id, 'role': args.role, 'display_name': args.name, 'kind': args.kind,
               'face': args.face, 'has_bleed': args.has_bleed}
    if args.layout:
        payload['layout'] = json.loads(project_path(args.layout).read_text(encoding='utf-8-sig'))
    if args.back_safety:
        payload['back_safety'] = json.loads(project_path(args.back_safety).read_text(encoding='utf-8-sig'))
    result = Library().import_candidate(payload, source.read_bytes())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
