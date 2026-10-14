import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';

/// Escaner de QR para pagar (HU15, E4-T13; sin pagina en `pantallas.fig`
/// — fila pendiente `billetera-qr-pago.png`, G76; UI desde tokens
/// `docs/20`).
///
/// - **Camara/lector:** area de vista previa del lector (placeholder
///   visual; la lectura real usa `camera`/`mobile_scanner` en el
///   dispositivo). Para probar sin camara, el campo de texto permite
///   **inyectar el `qr_content` leido** (mock del escaner): los tests
///   entran por aqui sin hardware.
/// - Al continuar, navega al resumen (`/wallet/pay-qr/confirm`) con el
///   contenido escaneado (o llama a [onScanned] si se provee, seam para
///   tests sin router).
/// - Cliente delgado: no interpreta el contenido (no valida firma ni
///   decodifica montos); solo lo transporta al resumen/pago.
///
/// Estados (docs/20 §7): contenido (campo + CTA), vacio (sin contenido +
/// error accionable al continuar), error inline en español. La carga
/// vive en el resumen/pago (el escaner no llama a red).
class QrScanPage extends StatefulWidget {
  const QrScanPage({super.key, this.onScanned, this.initialContent});

  /// Seam para tests: recibe el `qr_content` escaneado en vez de navegar.
  final ValueChanged<String>? onScanned;

  /// Contenido precargado (tests / re-escaneo).
  final String? initialContent;

  @override
  State<QrScanPage> createState() => _QrScanPageState();
}

class _QrScanPageState extends State<QrScanPage> {
  late final TextEditingController _controller;
  String? _error;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController(text: widget.initialContent ?? '');
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _continue() {
    final content = _controller.text.trim();
    if (content.isEmpty) {
      setState(
        () => _error =
            'Escanea el QR con la cámara o pega el contenido leído para continuar.',
      );
      return;
    }
    final onScanned = widget.onScanned;
    if (onScanned != null) {
      onScanned(content);
      return;
    }
    context.push('/wallet/pay-qr/confirm', extra: content);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Pagar con QR')),
      body: SingleChildScrollView(
        key: const Key('qr-pay-scan-scroll'),
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Apunta al QR del comercio. El comercio y el monto los '
              'confirma el servidor antes de mover tu dinero.',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
            ),
            const SizedBox(height: 12),
            // Vista previa del lector (placeholder visual; en el
            // dispositivo aqui vive la camara `camera`/`mobile_scanner`).
            Container(
              key: const Key('qr-pay-camera-placeholder'),
              width: double.infinity,
              height: 220,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                color: AppColors.surfaceContainerLow,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: AppColors.outlineVariant),
              ),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(
                    Icons.qr_code_scanner,
                    size: 56,
                    color: AppColors.primary,
                  ),
                  const SizedBox(height: 8),
                  Text(
                    'Lector QR',
                    style: AppTypography.titleMd.copyWith(fontSize: 16),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    'Centra el QR del comercio en el recuadro',
                    style: AppTypography.bodyMd.copyWith(
                      fontSize: 12,
                      color: AppColors.secondaryText,
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),
            AppCard(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Contenido leído (para pruebas sin cámara)',
                    style: AppTypography.labelMd.copyWith(
                      color: AppColors.primary,
                    ),
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    key: const Key('qr-pay-scan-input'),
                    controller: _controller,
                    maxLines: 3,
                    minLines: 1,
                    decoration: InputDecoration(
                      hintText: 'BO-QR1… (lo que leyó el escáner)',
                      filled: true,
                      fillColor: AppColors.surfaceContainerLowest,
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                    ),
                    onChanged: (_) {
                      if (_error != null) setState(() => _error = null);
                    },
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: 8),
                    Text(
                      _error!,
                      key: const Key('qr-pay-scan-error'),
                      style: AppTypography.labelMd.copyWith(
                        fontSize: 13,
                        color: AppColors.onErrorContainer,
                      ),
                    ),
                  ],
                  const SizedBox(height: 12),
                  SizedBox(
                    width: double.infinity,
                    height: 52,
                    child: FilledButton.icon(
                      key: const Key('qr-pay-scan-continue'),
                      onPressed: _continue,
                      icon: const Icon(Icons.arrow_forward, size: 20),
                      label: const Text(
                        'Continuar al resumen',
                        style: TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
