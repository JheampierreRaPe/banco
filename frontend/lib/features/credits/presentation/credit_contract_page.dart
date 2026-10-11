import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/credit_contract_service.dart';
import '../models/credit_contract.dart';
import '../models/loan_application.dart';
import '../utils/format.dart';

/// Pantalla del contrato de credito (E3-T15, HU11; spec
/// `docs/design/creditos-pantallas.md` §5, mockup `creditos-contrato.png`
/// + Fig pagina `Créditos/Contrato` `0:6716`, frame raiz `0:6717`).
///
/// - Fila expediente + badge `Aprobado` (`success-container`).
/// - Hero de condiciones aprobadas TAL CUAL las informa el servidor (monto
///   y plazo del expediente; la cuota/TCEA viven en los documentos: el
///   cliente no calcula ni redondea nada).
/// - Card Documentos (3): contrato, pagare y hoja resumen, cada uno con
///   `Abrir` (descarga el PDF del backend).
/// - Card Aviso legal (texto legal completo) + CTA `Firmar contrato` (va a
///   `/credits/sign`) + CTA `Descargar documentos (PDF)`.
/// - 4 estados (`docs/20` §7) con `LoadingView`/`EmptyView`/`ErrorView`.
/// - Sin `Idempotency-Key` en lectura/descarga (no crea recursos).
///
/// La pagina recibe el [applicationId] para `GET .../contract` y, cuando
/// viene desde el Estado, el [application] con las condiciones aprobadas
/// para el hero. Sin id muestra el estado vacio.
class CreditContractPage extends StatefulWidget {
  const CreditContractPage({
    super.key,
    this.service,
    this.applicationId,
    this.application,
    this.initialBundle,
  });

  final CreditContractServiceBase? service;
  final String? applicationId;
  final LoanApplicationResult? application;
  final ContractBundle? initialBundle;

  @override
  State<CreditContractPage> createState() => _CreditContractPageState();
}

class _CreditContractPageState extends State<CreditContractPage> {
  ContractBundle? _bundle;
  ApiException? _error;
  bool _loading = false;
  bool _downloadingAll = false;
  final Set<String> _downloading = {};

  String get _applicationId => widget.initialBundle?.applicationId.isNotEmpty ??
          false
      ? widget.initialBundle!.applicationId
      : (widget.application?.id.isNotEmpty ?? false
          ? widget.application!.id
          : (widget.applicationId ?? ''));

  @override
  void initState() {
    super.initState();
    _bundle = widget.initialBundle;
    if (_bundle == null && _applicationId.isNotEmpty) {
      _fetch();
    } else if (_bundle == null && _applicationId.isEmpty) {
      // Sin expediente (navegacion directa): estado vacio, sin fetch.
    } else if (_bundle == null) {
      _error = ApiException(
        code: 'UNKNOWN',
        message: 'Servicio de créditos no configurado.',
      );
    }
  }

  Future<void> _fetch() async {
    final service = widget.service;
    final id = _applicationId;
    if (id.isEmpty) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    if (service == null) {
      setState(() {
        _loading = false;
        _error = ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de créditos no configurado.',
        );
      });
      return;
    }
    try {
      final bundle = await service.getContract(id);
      if (!mounted) return;
      setState(() {
        _bundle = bundle;
        _error = null;
        _loading = false;
      });
    } on ApiException catch (error) {
      if (!mounted) return;
      setState(() {
        _error = error;
        _loading = false;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _error = ApiException(
          code: 'UNKNOWN',
          message: 'Ocurrió un error inesperado. Inténtalo más tarde.',
        );
        _loading = false;
      });
    }
  }

  void _announce(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message)),
    );
  }

  Future<void> _openDocument(ContractDocument doc) async {
    final service = widget.service;
    if (service == null) {
      _announce('Servicio de créditos no configurado.');
      return;
    }
    setState(() => _downloading.add(doc.doc));
    try {
      final pdf = await service.downloadDocument(_applicationId, doc.doc);
      _announce(
        'Documento descargado: ${pdf.suggestedFilename} '
        '(${pdf.bytes.length} bytes).',
      );
    } on ApiException catch (error) {
      _announce(creditContractUserMessage(error));
    } catch (_) {
      _announce('Ocurrió un error inesperado. Inténtalo más tarde.');
    } finally {
      if (mounted) setState(() => _downloading.remove(doc.doc));
    }
  }

  Future<void> _downloadAll(List<ContractDocument> docs) async {
    final service = widget.service;
    if (service == null) {
      _announce('Servicio de créditos no configurado.');
      return;
    }
    setState(() => _downloadingAll = true);
    var ok = 0;
    String? lastMessage;
    for (final doc in docs) {
      try {
        await service.downloadDocument(_applicationId, doc.doc);
        ok++;
      } on ApiException catch (error) {
        lastMessage = creditContractUserMessage(error);
      } catch (_) {
        lastMessage = 'Ocurrió un error inesperado. Inténtalo más tarde.';
      }
    }
    if (!mounted) return;
    setState(() => _downloadingAll = false);
    if (ok == docs.length) {
      _announce('Se descargaron los $ok documentos (PDF).');
    } else if (ok > 0) {
      _announce('Se descargaron $ok de ${docs.length} documentos. '
          '${lastMessage ?? ''}');
    } else {
      _announce(lastMessage ?? 'No se pudo descargar. Inténtalo más tarde.');
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Contrato de crédito')),
      body: _buildBody(),
    );
  }

  Widget _buildBody() {
    final bundle = _bundle;
    final error = _error;
    if (_loading && bundle == null) {
      return const LoadingView(
        key: Key('contract-loading'),
        message: 'Cargando los documentos del contrato…',
      );
    }
    if (error != null && bundle == null) {
      return ErrorView(
        key: const Key('contract-error'),
        message: creditContractUserMessage(error),
        onRetry: _fetch,
      );
    }
    if (bundle == null || bundle.isEmpty) {
      return EmptyView(
        key: const Key('contract-empty'),
        message: 'Aún no hay documentos generados para este expediente. '
            'Vuelve al estado para verificar la aprobación.',
        actionLabel: 'Volver al estado',
        onAction: () => context.pop(),
      );
    }
    return RefreshIndicator(
      onRefresh: _fetch,
      child: ListView(
        key: const Key('contract-content'),
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          if (_loading) const LinearProgressIndicator(),
          _expedientRow(bundle),
          const SizedBox(height: AppSpacing.stackMd),
          _conditionsHero(),
          const SizedBox(height: AppSpacing.stackMd),
          _documentsCard(bundle),
          const SizedBox(height: AppSpacing.stackMd),
          _legalCard(),
          const SizedBox(height: AppSpacing.stackMd),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              key: const Key('contract-sign-cta'),
              onPressed: () => context.push(
                '/credits/sign',
                extra: {
                  'applicationId': _applicationId,
                  'summary': _summaryLine(),
                },
              ),
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Firmar contrato'),
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(
              key: const Key('contract-download-cta'),
              onPressed: _downloadingAll
                  ? null
                  : () => _downloadAll(bundle.documents),
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(88, 52),
              ),
              child: Text(
                _downloadingAll
                    ? 'Descargando…'
                    : 'Descargar documentos (PDF)',
              ),
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Cargando, vacío y error (§7). Sin documentos: EmptyView.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
        ],
      ),
    );
  }

  // --fila_expediente_+_badge---------------------------------------------------

  Widget _expedientRow(ContractBundle bundle) {
    return Row(
      children: [
        Expanded(
          child: Text(
            'EXPEDIENTE ${bundle.applicationId.isEmpty ? 'EN CURSO' : bundle.applicationId}',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.onSurface,
            ),
          ),
        ),
        Container(
          key: const Key('contract-badge'),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          decoration: BoxDecoration(
            color: AppColors.successContainer,
            borderRadius: BorderRadius.circular(999),
          ),
          child: Text(
            bundle.isSigned ? 'Firmado' : 'Aprobado',
            style: AppTypography.labelMd.copyWith(
              color: AppColors.onSuccessContainer,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ],
    );
  }

  // --hero_de_condiciones_(tal_cual_del_servidor)--------------------------------

  /// Linea de condiciones con valores del servidor (sin recalcular).
  ///
  /// El expediente trae monto y plazo; la cuota/TCEA viven en los documentos
  /// del contrato y no se duplican aqui (cliente delgado).
  String _summaryLine() {
    final app = widget.application;
    final amount = app?.requestedAmountMinor;
    final term = app?.termMonths;
    if (amount != null && term != null) {
      return '${formatMinor(amount, 'PEN')} • '
          '$term ${term == 1 ? 'mes' : 'meses'}';
    }
    return 'Condiciones aprobadas en los documentos del servidor.';
  }

  Widget _conditionsHero() {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLow,
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'CONDICIONES APROBADAS (servidor)',
            style: AppTypography.labelSm.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.onSurfaceVariant,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            key: const Key('contract-conditions'),
            _summaryLine(),
            style: AppTypography.titleMd.copyWith(
              color: AppColors.primary,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Montos del servidor. El cliente no calcula.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
        ],
      ),
    );
  }

  // --card_de_documentos_(3)------------------------------------------------------

  Widget _documentsCard(ContractBundle bundle) {
    final docs = bundle.documents;
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Documentos (${docs.length})',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          for (var i = 0; i < docs.length; i++) ...[
            if (i > 0) const Divider(color: AppColors.divider),
            _documentRow(docs[i], i),
          ],
        ],
      ),
    );
  }

  Widget _documentRow(ContractDocument doc, int index) {
    final busy = _downloading.contains(doc.doc);
    return Padding(
      key: Key('contract-doc-$index'),
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  doc.displayName,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.onSurface,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  doc.displayMeta,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
              ],
            ),
          ),
          TextButton(
            key: Key('contract-open-${doc.doc}'),
            onPressed: busy ? null : () => _openDocument(doc),
            child: Text(
              busy ? 'Abriendo…' : 'Abrir',
              style: AppTypography.labelMd.copyWith(
                color: AppColors.accentText,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }

  // --aviso_legal------------------------------------------------------------------

  Widget _legalCard() {
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Aviso legal',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Lee los 3 documentos antes de firmar. Al firmar aceptas las '
            'condiciones aprobadas. La copia certificada se enviará a tu '
            'correo.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSurface,
            ),
          ),
        ],
      ),
    );
  }
}
