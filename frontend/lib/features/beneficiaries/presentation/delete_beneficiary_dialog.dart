import 'package:flutter/material.dart';

import '../../../core/theme/app_colors.dart';

/// Dialogo de confirmacion de eliminacion (mockup `Alta 5 - Eliminar`).
///
/// - Titulo `¿Eliminar beneficiario?` + `Esta acción no se puede revertir`.
/// - Cuerpo con el alias (dato ya visible en lista; sin CCI/cuenta completa).
/// - `Cancelar` (false) / `Sí, eliminar` (true, rojo).
class DeleteBeneficiaryDialog extends StatelessWidget {
  const DeleteBeneficiaryDialog({super.key, required this.alias});

  final String alias;

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 40,
                height: 40,
                decoration: const BoxDecoration(
                  color: AppColors.errorContainer,
                  shape: BoxShape.circle,
                ),
                child: const Icon(
                  Icons.delete_outline,
                  color: AppColors.error,
                ),
              ),
              const SizedBox(width: 12),
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '¿Eliminar beneficiario?',
                      style: TextStyle(fontWeight: FontWeight.bold),
                    ),
                    Text(
                      'Esta acción no se puede revertir',
                      style: TextStyle(
                        fontSize: 12,
                        color: AppColors.outline,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Text(
            '¿Estás seguro de eliminar a $alias de tus contactos '
            'rápidos? Sus datos guardados y topes pre-aprobados serán '
            'borrados.',
            style: const TextStyle(
              fontSize: 12,
              color: AppColors.onSurfaceVariant,
            ),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: OutlinedButton(
                  key: const Key('delete-cancel'),
                  onPressed: () => Navigator.of(context).pop(false),
                  child: const Text('Cancelar'),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: FilledButton(
                  key: const Key('delete-confirm'),
                  style: FilledButton.styleFrom(
                    backgroundColor: AppColors.error,
                  ),
                  onPressed: () => Navigator.of(context).pop(true),
                  child: const Text('Sí, eliminar'),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
